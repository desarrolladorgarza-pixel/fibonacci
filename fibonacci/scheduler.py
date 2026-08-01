"""
FIBONACCI — Programador.

Tareas recurrentes: "cada mañana revisa mis PRs abiertos y mándame el resumen
por Telegram", "cada domingo respalda ~/proyectos".

## Por qué no un cron externo

Podría delegarse a cron o systemd timers, y de hecho `deploy/` los ofrece. Pero
un scheduler propio da tres cosas que un cron no:

  - **Presupuesto por ejecución.** Un cron que dispara un agente sin techo es
    la forma más eficiente de despertar con una factura.
  - **Registro en el journal.** Lo que hace una tarea programada es tan
    reversible y auditable como lo que haces tú a mano.
  - **Portabilidad.** El mismo `fib schedule` funciona en Windows y Termux,
    donde cron no existe o no sobrevive al gestor de batería.

## Autonomía de lo desatendido

Una tarea programada corre sin nadie mirando, así que **no puede confirmar
nada**. Lo que exigiría confirmación se salta y se reporta en el resultado.
Si quieres que una tarea desatendida haga algo que requiere confirmación,
declara el ámbito como libre — explícitamente, de antemano, sabiendo qué
concedes.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .platform import data_dir
from .store import Store

log = logging.getLogger("fibonacci.scheduler")

MIGRATIONS = [
    (1, "esquema inicial", """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, instruction TEXT NOT NULL,
            schedule TEXT NOT NULL, surface TEXT, channel TEXT,
            session TEXT, budget_usd REAL DEFAULT 0.5,
            enabled INTEGER DEFAULT 1, last_run REAL DEFAULT 0,
            next_run REAL DEFAULT 0, runs INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0, created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_next ON jobs(enabled, next_run);

        CREATE TABLE IF NOT EXISTS runs_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, ts REAL,
            ok INTEGER, output TEXT, cost REAL, elapsed_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_runslog_job ON runs_log(job_id, ts);
    """),
]


@dataclass
class Job:
    name: str
    instruction: str
    schedule: str                     # "diario 07:00" | "cada 30m" | cron
    surface: str = ""                 # a dónde entregar el resultado
    channel: str = ""
    session: str = ""
    budget_usd: float = 0.5
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    runs: int = 0
    failures: int = 0
    id: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.id:
            import uuid
            self.id = f"job_{uuid.uuid4().hex[:10]}"
        if not self.session:
            self.session = f"programado:{self.name}"


# ---------------------------------------------------------------------------
# Horarios en lenguaje natural
# ---------------------------------------------------------------------------

DIAS = {"lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3,
        "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6}


def next_run(schedule: str, desde: float | None = None) -> float:
    """
    Acepta expresiones naturales en español y cron de 5 campos.

        "cada 30m"        "cada 2h"        "cada dia 07:00"
        "diario 07:00"    "lunes 09:30"    "0 7 * * *"
    """
    ahora = datetime.fromtimestamp(desde or time.time())
    s = schedule.strip().lower()

    # Intervalos: cada N (m|h|d)
    m = re.match(r"cada\s+(\d+)\s*(m|min|minutos?|h|horas?|d|d[ií]as?)$", s)
    if m:
        n, unidad = int(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n)}[unidad]
        return (ahora + delta).timestamp()

    # Diario a una hora
    m = re.match(r"(?:diario|cada\s+d[ií]a)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        obj = ahora.replace(hour=h, minute=mi, second=0, microsecond=0)
        if obj <= ahora:
            obj += timedelta(days=1)
        return obj.timestamp()

    # Día de la semana a una hora
    m = re.match(r"(\w+)\s+(\d{1,2}):(\d{2})$", s)
    if m and m.group(1) in DIAS:
        objetivo = DIAS[m.group(1)]
        h, mi = int(m.group(2)), int(m.group(3))
        dias = (objetivo - ahora.weekday()) % 7
        obj = (ahora + timedelta(days=dias)).replace(
            hour=h, minute=mi, second=0, microsecond=0)
        if obj <= ahora:
            obj += timedelta(days=7)
        return obj.timestamp()

    # Cron de 5 campos (subconjunto: minuto, hora, día-mes, mes, día-semana)
    if len(s.split()) == 5:
        return _next_cron(s, ahora)

    raise ValueError(
        f"horario no reconocido: '{schedule}'. Ejemplos: 'cada 30m', "
        "'diario 07:00', 'lunes 09:30', '0 7 * * *'")


def _next_cron(expr: str, desde: datetime) -> float:
    minuto, hora, dom, mes, dow = expr.split()

    def coincide(campo: str, valor: int, minimo: int = 0) -> bool:
        if campo == "*":
            return True
        for parte in campo.split(","):
            if parte.startswith("*/"):
                if (valor - minimo) % int(parte[2:]) == 0:
                    return True
            elif "-" in parte:
                a, b = parte.split("-")
                if int(a) <= valor <= int(b):
                    return True
            elif int(parte) == valor:
                return True
        return False

    t = desde.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):        # techo: un año
        if (coincide(minuto, t.minute) and coincide(hora, t.hour)
                and coincide(dom, t.day, 1) and coincide(mes, t.month, 1)
                and coincide(dow, t.weekday() if t.weekday() < 6 else 0)):
            return t.timestamp()
        t += timedelta(minutes=1)
    raise ValueError(f"la expresión cron '{expr}' no coincide en un año")


# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "schedule.db"
        self.store = Store(self.path, MIGRATIONS)
        self._stop = threading.Event()

    # -- CRUD -------------------------------------------------------------

    def add(self, job: Job) -> Job:
        job.next_run = next_run(job.schedule)      # valida al crear, no al correr
        self.store.write(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, job.name, job.instruction, job.schedule, job.surface,
             job.channel, job.session, job.budget_usd, int(job.enabled),
             job.last_run, job.next_run, job.runs, job.failures, job.created_at))
        log.info("Tarea programada '%s' -> %s", job.name,
                 datetime.fromtimestamp(job.next_run).strftime("%Y-%m-%d %H:%M"))
        return job

    def list(self, only_enabled: bool = False) -> list[Job]:
        sql = "SELECT * FROM jobs"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY next_run"
        return [_row(r) for r in self.store.all(sql)]

    def get(self, ident: str) -> Job | None:
        r = self.store.one("SELECT * FROM jobs WHERE id=? OR name=?", (ident, ident))
        return _row(r) if r else None

    def remove(self, ident: str) -> bool:
        cur = self.store.write("DELETE FROM jobs WHERE id=? OR name=?", (ident, ident))
        return cur.rowcount > 0

    def toggle(self, ident: str, enabled: bool) -> bool:
        cur = self.store.write(
            "UPDATE jobs SET enabled=? WHERE id=? OR name=?",
            (int(enabled), ident, ident))
        return cur.rowcount > 0

    def due(self, ahora: float | None = None) -> list[Job]:
        t = ahora or time.time()
        return [_row(r) for r in self.store.all(
            "SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run",
            (t,))]

    def history(self, job_id: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.store.all(
            "SELECT * FROM runs_log WHERE job_id=? ORDER BY ts DESC LIMIT ?",
            (job_id, limit))]

    # -- ejecución --------------------------------------------------------

    def execute(self, job: Job, agent, deliver=None) -> dict:
        """
        Corre una tarea. Sin humano disponible, así que lo que exigiría
        confirmación se salta y se reporta.
        """
        t0 = time.time()
        saltadas: list[str] = []

        prev_confirm = agent.tools.confirm
        prev_budget = agent.budget.max_usd
        agent.tools.confirm = lambda desc, danger: (saltadas.append(desc[:120]), False)[1]
        agent.budget.max_usd = job.budget_usd

        try:
            reply = agent.chat(job.instruction, job.session, surface="programado")
            texto, ok = reply.text, True
            costo = reply.cost_usd
        except Exception as exc:  # noqa: BLE001
            log.exception("Tarea '%s' falló", job.name)
            texto, ok, costo = f"Falló: {exc}", False, 0.0
        finally:
            agent.tools.confirm = prev_confirm
            agent.budget.max_usd = prev_budget

        if saltadas:
            texto += ("\n\n⚠ Omití acciones que requieren confirmación "
                      "(nadie estaba mirando):\n"
                      + "\n".join(f"· {s}" for s in saltadas[:3]))

        elapsed = int((time.time() - t0) * 1000)
        self._record(job, ok, texto, costo, elapsed)

        if deliver and job.surface:
            try:
                deliver(job, texto)
            except Exception as exc:  # noqa: BLE001
                log.error("No se pudo entregar '%s': %s", job.name, exc)

        return {"job": job.name, "ok": ok, "texto": texto,
                "costo": costo, "ms": elapsed, "omitidas": len(saltadas)}

    def _record(self, job: Job, ok: bool, output: str, cost: float,
                elapsed: int) -> None:
        ahora = time.time()
        self.store.write(
            "INSERT INTO runs_log(job_id, ts, ok, output, cost, elapsed_ms) "
            "VALUES (?,?,?,?,?,?)",
            (job.id, ahora, int(ok), output[:8000], cost, elapsed))

        try:
            siguiente = next_run(job.schedule, ahora)
        except ValueError:
            siguiente = ahora + 3600

        fallos = job.failures + (0 if ok else 1)
        # Una tarea que falla cinco veces seguidas se desactiva sola. Seguir
        # reintentando algo roto solo acumula ruido y gasto.
        enabled = job.enabled and fallos < 5
        if not enabled and job.enabled:
            log.warning("Tarea '%s' desactivada tras %d fallos", job.name, fallos)

        self.store.write(
            "UPDATE jobs SET last_run=?, next_run=?, runs=runs+1, failures=?, "
            "enabled=? WHERE id=?",
            (ahora, siguiente, 0 if ok else fallos, int(enabled), job.id))

    # -- bucle ------------------------------------------------------------

    def serve(self, agent, deliver=None, tick: float = 30.0) -> None:
        """Bucle del demonio. `fib schedule serve`."""
        log.info("Programador activo (%d tareas)", len(self.list(True)))
        while not self._stop.is_set():
            for job in self.due():
                log.info("Ejecutando '%s'", job.name)
                self.execute(job, agent, deliver)
            self._stop.wait(tick)

    def stop(self) -> None:
        self._stop.set()


def _row(r) -> Job:
    return Job(id=r["id"], name=r["name"], instruction=r["instruction"],
               schedule=r["schedule"], surface=r["surface"] or "",
               channel=r["channel"] or "", session=r["session"] or "",
               budget_usd=r["budget_usd"], enabled=bool(r["enabled"]),
               last_run=r["last_run"], next_run=r["next_run"],
               runs=r["runs"], failures=r["failures"],
               created_at=r["created_at"] or 0)
