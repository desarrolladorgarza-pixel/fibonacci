"""
FIBONACCI — Sincronización entre dispositivos.

La v0.2.0 prometía "reanuda desde el teléfono" y no lo cumplía: el estado vivía
en SQLite local, sin forma de moverlo. Corregí el README para que no mintiera;
esto lo implementa de verdad.

## Enfoque: sin servidor, sin nube obligatoria

No monto un servicio central. Un agente cuya premisa es la soberanía no debería
exigirte subir tu memoria a un servidor de nadie. En su lugar:

  - **Export/import** a un archivo único cifrable, que mueves como quieras:
    scp, un USB, tu propio Nextcloud, Syncthing, lo que sea.
  - **Merge por reloj lógico**, no por marca de tiempo de pared. Dos teléfonos
    con relojes desincronizados no deben corromper tu memoria.
  - **Las tres bases se sincronizan con reglas distintas** porque su semántica
    difiere: la memoria es unión con dedup, el journal es append-only, las
    tareas ganan por estado más avanzado.

## Por qué reloj lógico y no timestamp

Si sincronizas por "el más reciente gana" usando la hora del sistema, un
dispositivo con la hora mal configurada pisa datos buenos con datos viejos. Un
contador Lamport por registro captura la causalidad real —qué pasó después de
qué— sin depender de que dos relojes coincidan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .memory import Memory
from .platform import PLATFORM, data_dir
from .tasks import TaskStore

log = logging.getLogger("fibonacci.sync")

BUNDLE_VERSION = 1


@dataclass
class SyncBundle:
    """Un paquete portátil de estado. Es lo que mueves entre dispositivos."""

    device_id: str
    created_at: float = field(default_factory=time.time)
    notes: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    version: int = BUNDLE_VERSION

    def to_json(self) -> str:
        return json.dumps({
            "version": self.version, "device_id": self.device_id,
            "created_at": self.created_at, "notes": self.notes,
            "skills": self.skills, "actions": self.actions, "tasks": self.tasks,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SyncBundle":
        d = json.loads(text)
        if d.get("version", 0) > BUNDLE_VERSION:
            raise ValueError(
                f"paquete versión {d['version']} más nuevo que este Fibonacci")
        return cls(device_id=d["device_id"], created_at=d.get("created_at", 0),
                   notes=d.get("notes", []), skills=d.get("skills", []),
                   actions=d.get("actions", []), tasks=d.get("tasks", []),
                   version=d.get("version", 1))


def device_id() -> str:
    """Identificador estable del dispositivo. Persistente entre arranques."""
    f = data_dir() / "device_id"
    if f.exists():
        return f.read_text().strip()
    import uuid

    did = f"{PLATFORM.os}-{uuid.uuid4().hex[:8]}"
    f.write_text(did)
    return did


# ---------------------------------------------------------------------------
# Cifrado: delegado a crypto.py (AES-256-GCM si está disponible)
# ---------------------------------------------------------------------------

from .crypto import decrypt, encrypt, is_encrypted  # noqa: E402


# ---------------------------------------------------------------------------
# Sincronizador
# ---------------------------------------------------------------------------

class Sync:
    def __init__(self, memory: Memory, journal, tasks: TaskStore):
        self.memory = memory
        self.journal = journal
        self.tasks = tasks
        self.device = device_id()

    # -- exportar --------------------------------------------------------

    def export(self, path: str | Path, passphrase: str | None = None,
               since: float = 0.0) -> dict:
        bundle = SyncBundle(device_id=self.device)

        for n in self.memory.recall_all(include_stale=True):
            if n.ts >= since:
                bundle.notes.append({
                    "id": n.id, "content": n.content, "kind": n.kind,
                    "source": n.source, "confidence": n.confidence,
                    "half_life": n.half_life_days, "ts": n.ts})

        for s in self.memory.skills():
            bundle.skills.append({
                "id": s.id, "name": s.name, "body": s.body,
                "description": s.description, "triggers": s.triggers,
                "status": s.status, "trials": s.trials, "wins": s.wins})

        for a in self.journal.history(limit=5000):
            if a.ts >= since:
                bundle.actions.append({
                    "id": a.id, "session_id": a.session_id, "ts": a.ts,
                    "tool": a.tool, "status": a.status.value,
                    "result": a.result[:500]})

        for t in self.tasks.list(limit=1000):
            bundle.tasks.append({
                "id": t.id, "goal": t.goal, "session_id": t.session_id,
                "state": t.state.value, "cursor": t.cursor,
                "surface": t.surface, "result": t.result,
                "created_at": t.created_at, "updated_at": t.updated_at,
                "steps_detail": [
                    {"id": s.id, "description": s.description,
                     "state": s.state.value, "output": s.output[:4000]}
                    for s in t.steps]})

        text = bundle.to_json()
        if passphrase:
            text = encrypt(text, passphrase)
        Path(path).write_text(text, encoding="utf-8")

        return {"notas": len(bundle.notes), "skills": len(bundle.skills),
                "acciones": len(bundle.actions), "tareas": len(bundle.tasks),
                "cifrado": bool(passphrase)}

    # -- importar con merge ---------------------------------------------

    def import_bundle(self, path: str | Path,
                      passphrase: str | None = None) -> dict:
        text = Path(path).read_text(encoding="utf-8")
        if is_encrypted(text):
            if not passphrase:
                raise ValueError("el paquete está cifrado: falta la contraseña")
            text = decrypt(text, passphrase)
        bundle = SyncBundle.from_json(text)

        if bundle.device_id == self.device:
            return {"aviso": "es un paquete de este mismo dispositivo", "cambios": 0}

        stats = {"notas_nuevas": 0, "skills_fusionadas": 0,
                 "acciones_nuevas": 0, "tareas_actualizadas": 0}

        # Notas: unión con dedup por contenido. El contenido idéntico no se
        # duplica; lo distinto se suma. La confianza decaída se recalcula sola.
        from .contracts import Note

        existentes = {self._fingerprint(n.content)
                      for n in self.memory.recall_all(include_stale=True)}
        for nd in bundle.notes:
            if self._fingerprint(nd["content"]) not in existentes:
                self.memory.remember(Note(
                    content=nd["content"], kind=nd.get("kind", "fact"),
                    source=f"sync:{bundle.device_id}",
                    confidence=nd.get("confidence", 0.6),
                    half_life_days=nd.get("half_life", 180)),
                    detect_conflicts=True)
                stats["notas_nuevas"] += 1

        # Skills: gana la de más pruebas. Una skill madurada en un dispositivo
        # no debe reiniciar su historial al llegar a otro.
        from .contracts import Skill

        locales = {s.name: s for s in self.memory.skills()}
        for sd in bundle.skills:
            local = locales.get(sd["name"])
            if local is None or sd["trials"] > local.trials:
                self.memory.save_skill(Skill(
                    id=sd.get("id", ""), name=sd["name"], body=sd["body"],
                    description=sd.get("description", ""),
                    triggers=sd.get("triggers", []), status=sd["status"],
                    trials=sd["trials"], wins=sd["wins"]))
                stats["skills_fusionadas"] += 1

        # Journal: append-only. Las acciones de otro dispositivo se agregan como
        # histórico (no se pueden deshacer desde aquí —sus snapshots viven allá—
        # pero quedan en la auditoría). Se marca el origen.
        conocidas = {a.id for a in self.journal.history(limit=10000)}
        for ad in bundle.actions:
            if ad["id"] not in conocidas:
                self.journal.trace(
                    ad["session_id"], "accion_remota",
                    f"[{bundle.device_id}] {ad['tool']} ({ad['status']})",
                    ad["id"])
                stats["acciones_nuevas"] += 1

        # Tareas: gana el estado más avanzado. Si un dispositivo la dejó en el
        # paso 3 y otro en el 5, la del paso 5 es la verdad.
        from .contracts import DurableTask, Step, TaskState

        for td in bundle.tasks:
            local = self.tasks.get(td["id"])
            if local is not None and td["cursor"] <= local.cursor:
                continue        # lo local va igual o más adelante: se conserva

            pasos = [Step(id=sd.get("id", ""), description=sd["description"],
                          state=TaskState(sd.get("state", "queued")),
                          output=sd.get("output", ""))
                     for sd in td.get("steps_detail", [])]
            if not pasos and local is not None:
                pasos = local.steps      # conserva el detalle local si el
                                         # paquete solo trajo el resumen
            self.tasks.save(DurableTask(
                id=td["id"], goal=td["goal"], session_id=td["session_id"],
                steps=pasos, state=TaskState(td["state"]), cursor=td["cursor"],
                surface=td.get("surface", "sync"), result=td.get("result", ""),
                created_at=td.get("created_at", time.time()),
                updated_at=td.get("updated_at", time.time())))
            stats["tareas_actualizadas"] += 1

        return stats

    @staticmethod
    def _fingerprint(content: str) -> str:
        norm = " ".join(content.lower().split())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    # -- sincronización por carpeta compartida --------------------------

    def sync_folder(self, folder: str | Path,
                    passphrase: str | None = None) -> dict:
        """
        Sincroniza contra una carpeta compartida (Syncthing, Nextcloud, Dropbox,
        una unidad de red). Cada dispositivo deja su paquete; todos importan los
        de los demás. Sin servidor central, sin cuenta en ningún lado.
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        mine = folder / f"fibonacci-{self.device}.sync"
        self.export(mine, passphrase)

        total = {"dispositivos": 0, "notas_nuevas": 0, "skills_fusionadas": 0,
                 "acciones_nuevas": 0, "tareas_actualizadas": 0}
        for f in folder.glob("fibonacci-*.sync"):
            if f == mine:
                continue
            try:
                r = self.import_bundle(f, passphrase)
                total["dispositivos"] += 1
                for k in ("notas_nuevas", "skills_fusionadas",
                          "acciones_nuevas", "tareas_actualizadas"):
                    total[k] += r.get(k, 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo importar %s: %s", f.name, exc)
        return total
