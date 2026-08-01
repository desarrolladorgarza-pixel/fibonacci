"""
FIBONACCI — Journal transaccional.

**Esta es la idea que justifica que Fibonacci exista.**

Los agentes actuales defienden *antes*: aprobación de comandos, aislamiento,
allowlists. Todo eso actúa antes del hecho y se degrada con el uso — tras
treinta confirmaciones uno dice que sí sin leer. Fibonacci añade la defensa
que falta: **posterior**. Cada mutación se registra con su inverso antes de
aplicarse, y `fib undo` la revierte.

## Seguridad del undo (corregido en 0.2.0)

La v0.1.0 revertía sin comprobar si el archivo había cambiado *después* de la
acción. Si el agente escribía, tú editabas a mano y luego hacías `undo`, tu
edición se perdía sin aviso.

**Un undo que destruye trabajo más nuevo en silencio es peor que no tener
undo.** Ahora cada acción guarda el hash de lo que dejó. Al revertir se
compara: si el estado actual no coincide, el undo se niega y explica por qué.
Con `--force` procede, pero solo después de decírtelo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from .contracts import Action, ActionStatus
from .platform import data_dir
from .store import Store

log = logging.getLogger("fibonacci.journal")

MIGRATIONS = [
    (1, "esquema inicial", """
        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ts REAL NOT NULL,
            tool TEXT NOT NULL, arguments TEXT NOT NULL,
            inverse_tool TEXT, inverse_arguments TEXT, snapshot TEXT,
            status TEXT NOT NULL, result TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_act_session ON actions(session_id, ts);
        CREATE INDEX IF NOT EXISTS idx_act_status ON actions(status, ts);
    """),
    (2, "hash de verificacion para undo seguro", """
        ALTER TABLE actions ADD COLUMN after_hash TEXT;
        ALTER TABLE actions ADD COLUMN target_path TEXT;
    """),
    (3, "traza de razonamiento", """
        CREATE TABLE IF NOT EXISTS traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,
            detail TEXT, action_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trace_session ON traces(session_id, ts);
    """),
]

MISSING = "<no-existe>"


def file_hash(path: str | Path) -> str:
    """Hash del contenido, o marca de inexistencia. Ambos estados importan:
    'el archivo no existe' es un estado valido que el undo debe reconocer."""
    p = Path(path)
    if not p.exists():
        return MISSING
    if p.is_dir():
        return "<dir>"
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Journal:
    def __init__(self, path: str | Path | None = None, snapshots: Path | None = None):
        self.path = Path(path) if path else data_dir() / "journal.db"
        self.snap_dir = Path(snapshots) if snapshots else data_dir() / "snapshots"
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.path, MIGRATIONS)
        self._undoers: dict[str, Callable[[Action], str]] = {}
        self._resolvers: dict[str, Callable[[Action], Path | None]] = {}

    @property
    def db(self):
        return self.store.db

    # ------------------------------------------------------------------

    def register_undoer(self, tool: str, fn: Callable[[Action], str],
                        target: Callable[[Action], Path | None] | None = None) -> None:
        """
        `target` dice que ruta observa la accion, para poder hashearla. Sin
        ella la accion se registra pero sin verificacion de integridad, y el
        undo avisa de esa limitacion en vez de asumir que todo esta bien.
        """
        self._undoers[tool] = fn
        if target:
            self._resolvers[tool] = target

    def snapshot_file(self, path: str | Path) -> str | None:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        if p.stat().st_size > 32 * 1024 * 1024:
            log.warning("Archivo muy grande para snapshot: %s", p)
            return None
        dest = self.snap_dir / f"{int(time.time()*1000)}_{p.name}"
        shutil.copy2(p, dest)
        return str(dest)

    # ------------------------------------------------------------------

    def record(self, action: Action, target_path: Path | None = None) -> Action:
        if target_path is None and action.tool in self._resolvers:
            try:
                target_path = self._resolvers[action.tool](action)
            except Exception:  # noqa: BLE001
                target_path = None

        after_hash = file_hash(target_path) if target_path else None

        if action.status == ActionStatus.PENDING:
            action.status = (
                ActionStatus.APPLIED
                if (action.tool in self._undoers or action.inverse_tool or action.snapshot)
                else ActionStatus.IRREVERSIBLE
            )

        self.store.write(
            "INSERT OR REPLACE INTO actions "
            "(id,session_id,ts,tool,arguments,inverse_tool,inverse_arguments,"
            " snapshot,status,result,after_hash,target_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (action.id, action.session_id, action.ts, action.tool,
             json.dumps(action.arguments, default=str), action.inverse_tool,
             json.dumps(action.inverse_arguments, default=str), action.snapshot,
             action.status.value, action.result[:5000], after_hash,
             str(target_path) if target_path else None),
        )
        return action

    def trace(self, session_id: str, kind: str, detail: str,
              action_id: str | None = None) -> None:
        """Registro del razonamiento, no solo del efecto. `fib history --trace`
        contesta *por que* el agente hizo algo, no solo *que* hizo."""
        self.store.write(
            "INSERT INTO traces(session_id, ts, kind, detail, action_id) "
            "VALUES (?,?,?,?,?)",
            (session_id, time.time(), kind, detail[:4000], action_id),
        )

    def traces(self, session_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM traces WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Undo con verificacion de integridad
    # ------------------------------------------------------------------

    def check_integrity(self, act: Action) -> tuple[bool, str]:
        """(seguro, motivo). Se consulta antes de revertir."""
        row = self.store.one(
            "SELECT after_hash, target_path FROM actions WHERE id=?", (act.id,))
        if row is None:
            return False, "accion no encontrada"
        after_hash, target = row["after_hash"], row["target_path"]

        if not after_hash or not target:
            return True, "sin verificacion disponible para esta herramienta"

        current = file_hash(target)
        if current == after_hash:
            return True, "intacto"

        name = Path(target).name
        if current == MISSING:
            return False, f"'{name}' ya no existe (lo borraste tu?)"
        if after_hash == MISSING:
            return False, f"'{name}' fue creado por alguien mas despues"
        return False, f"'{name}' fue modificado despues de esta accion"

    def undo_last(self, session_id: str | None = None,
                  force: bool = False) -> tuple[bool, str]:
        act = self._latest_undoable(session_id)
        if act is None:
            return False, "No hay nada que deshacer."
        return self._undo(act, force=force)

    def undo_action(self, action_id: str, force: bool = False) -> tuple[bool, str]:
        r = self.store.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if r is None:
            return False, f"Accion desconocida: {action_id}"
        return self._undo(_row_to_action(r), force=force)

    def undo_session(self, session_id: str, force: bool = False) -> tuple[int, list[str]]:
        """
        Revierte en orden inverso: el estado se reconstruye hacia atras.

        Si una accion falla la verificacion, se detiene ahi en vez de seguir.
        Revertir salteado deja el sistema en un estado que nadie pidio: peor
        que revertir de menos.
        """
        rows = self.store.all(
            "SELECT * FROM actions WHERE session_id=? AND status=? ORDER BY ts DESC",
            (session_id, ActionStatus.APPLIED.value))
        done, notes = 0, []
        for r in rows:
            act = _row_to_action(r)
            ok, msg = self._undo(act, force=force)
            notes.append(msg)
            if ok:
                done += 1
            elif not force:
                notes.append("  detenido aqui: revertir salteado dejaria un estado "
                             "inconsistente. Usa --force para continuar.")
                break
        return done, notes

    def _undo(self, act: Action, force: bool = False) -> tuple[bool, str]:
        if act.status != ActionStatus.APPLIED:
            return False, f"{act.tool}: no revertible ({act.status.value})"

        safe, reason = self.check_integrity(act)
        if not safe and not force:
            return False, (f"{act.tool}: {reason}. Revertir borraria ese cambio. "
                           "Usa --force si aun asi lo quieres.")

        try:
            if act.tool in self._undoers:
                msg = self._undoers[act.tool](act)
            elif act.inverse_tool and act.inverse_tool in self._undoers:
                msg = self._undoers[act.inverse_tool](act)
            elif act.snapshot and Path(act.snapshot).exists():
                target = act.arguments.get("path") or act.arguments.get("file")
                if not target or not Path(target).is_absolute():
                    return False, (f"{act.tool}: snapshot sin destino resoluble "
                                   "(la herramienta debe registrar un undoer)")
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(act.snapshot, target)
                msg = f"restaurado {target}"
            else:
                return False, f"{act.tool}: sin forma conocida de revertir"

            self.store.write("UPDATE actions SET status=? WHERE id=?",
                             (ActionStatus.UNDONE.value, act.id))
            log.info("Deshecho %s (%s)", act.id, act.tool)
            warn = "  [forzado sobre un cambio posterior]" if not safe else ""
            return True, f"{act.tool}: {msg}{warn}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{act.tool}: fallo al deshacer - {exc}"

    def _latest_undoable(self, session_id: str | None) -> Action | None:
        sql = "SELECT * FROM actions WHERE status=?"
        params: list = [ActionStatus.APPLIED.value]
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY ts DESC LIMIT 1"
        r = self.store.one(sql, params)
        return _row_to_action(r) if r else None

    # ------------------------------------------------------------------

    def history(self, session_id: str | None = None, limit: int = 30) -> list[Action]:
        sql, params = "SELECT * FROM actions", []
        if session_id:
            sql += " WHERE session_id=?"
            params.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [_row_to_action(r) for r in self.store.all(sql, params)]

    def stats(self) -> dict:
        total = self.store.scalar("SELECT COUNT(*) FROM actions")
        irr = self.store.scalar("SELECT COUNT(*) FROM actions WHERE status=?",
                                (ActionStatus.IRREVERSIBLE.value,))
        verificables = self.store.scalar(
            "SELECT COUNT(*) FROM actions WHERE after_hash IS NOT NULL")
        return {
            "acciones": total,
            "reversibles": self.store.scalar(
                "SELECT COUNT(*) FROM actions WHERE status=?",
                (ActionStatus.APPLIED.value,)),
            "deshechas": self.store.scalar(
                "SELECT COUNT(*) FROM actions WHERE status=?",
                (ActionStatus.UNDONE.value,)),
            "irreversibles": irr,
            "cobertura_undo": f"{(1 - irr/total)*100:.0f}%" if total else "n/a",
            "con_verificacion": f"{verificables/total*100:.0f}%" if total else "n/a",
        }

    def prune_snapshots(self, older_than_days: float = 14.0) -> int:
        cutoff = time.time() - older_than_days * 86400
        # Nunca podar un snapshot del que aun depende un undo pendiente.
        vivos = {
            r["snapshot"] for r in self.store.all(
                "SELECT snapshot FROM actions WHERE status=? AND snapshot IS NOT NULL",
                (ActionStatus.APPLIED.value,))
        }
        n = 0
        for f in self.snap_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff and str(f) not in vivos:
                f.unlink()
                n += 1
        return n


def _row_to_action(r) -> Action:
    return Action(
        id=r["id"], session_id=r["session_id"], ts=r["ts"], tool=r["tool"],
        arguments=json.loads(r["arguments"]), inverse_tool=r["inverse_tool"],
        inverse_arguments=json.loads(r["inverse_arguments"] or "{}"),
        snapshot=r["snapshot"], status=ActionStatus(r["status"]),
        result=r["result"] or "",
    )
