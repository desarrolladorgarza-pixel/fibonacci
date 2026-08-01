"""
FIBONACCI — Subagentes.

Hermes delega en subagentes aislados y paraleliza. Fibonacci añade lo que
falta: **el journal es compartido**, así que `fib undo --all` revierte el
árbol completo de trabajo, no solo lo que hizo el agente principal.

Sin eso, la delegación rompe la garantía central del producto. Un subagente
que escribe veinte archivos y no aparece en el journal del padre es un agujero
por donde se escapa toda la reversibilidad.

## Aislamiento

Cada subagente recibe:
  - su propio `session_id` derivado del padre (`s1/sub-3`), para trazabilidad;
  - **su propia contaminación** (`TaintState`), para que la web que leyó uno
    no bloquee la salida de otro sin motivo;
  - un ámbito de trabajo que no puede exceder el del padre;
  - un trozo del presupuesto, no el presupuesto entero.

El reparto de presupuesto importa: sin él, cinco subagentes en paralelo gastan
cinco veces el techo que autorizaste. Es el error más común al paralelizar.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .agent import Agent, AgentReply, SpendBudget
from .contracts import Capability
from .security import EgressPolicy, TaintState
from .tools import ToolBox

log = logging.getLogger("fibonacci.subagents")


@dataclass
class SubTask:
    instruction: str
    name: str = ""
    capability: Capability | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class SubResult:
    name: str
    ok: bool
    text: str
    tools_used: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    error: str = ""


class Swarm:
    """
    Ejecuta subagentes en paralelo compartiendo journal y memoria, pero con
    contaminación y presupuesto propios.
    """

    def __init__(self, parent: Agent, max_parallel: int = 4):
        self.parent = parent
        self.max_parallel = max_parallel
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _spawn(self, session_id: str, share: float) -> Agent:
        """
        Un agente hijo. Comparte journal (para el undo del árbol) y memoria
        (para que lo aprendido sirva a todos), pero NO comparte contaminación:
        de otro modo, que un subagente lea una web bloquearía la salida de los
        demás sin razón.
        """
        pb = self.parent.budget
        child_budget = SpendBudget(
            max_usd=pb.max_usd * share,
            max_tokens=int(pb.max_tokens * share),
            max_seconds=pb.max_seconds,
        )
        child_tools = ToolBox(
            self.parent.journal,
            root=self.parent.tools.root,           # nunca excede el ámbito del padre
            confirm=self.parent.tools.confirm,
            taint=TaintState(),
            egress=EgressPolicy(
                allowed_hosts=set(self.parent.tools.egress.allowed_hosts)),
            max_result_chars=self.parent.tools.max_result_chars,
        )
        return Agent(
            self.parent.mesh, self.parent.memory, self.parent.journal,
            child_tools, persona=SUB_PERSONA,
            on_event=lambda k, p: self.parent.on_event(k, f"[{session_id}] {p}"),
            budget=child_budget,
        )

    def run(self, tasks: list[SubTask], session_id: str) -> list[SubResult]:
        """
        Ejecuta en paralelo respetando dependencias. Devuelve resultados en el
        orden de entrada.
        """
        if not tasks:
            return []

        for i, t in enumerate(tasks):
            if not t.name:
                t.name = f"sub-{i + 1}"

        share = 1.0 / max(len(tasks), 1)
        results: dict[str, SubResult] = {}
        pending = {t.name: t for t in tasks}

        self.parent.journal.trace(
            session_id, "enjambre",
            f"{len(tasks)} subagentes, {share:.0%} del presupuesto cada uno")

        # Ondas topológicas: cada ola solo contiene tareas cuyas dependencias
        # ya se resolvieron.
        wave = 0
        while pending:
            wave += 1
            listos = [t for t in pending.values()
                      if all(d in results for d in t.depends_on)]
            if not listos:
                for t in pending.values():
                    results[t.name] = SubResult(
                        t.name, False, "", error=(
                            "dependencia circular o no satisfecha: "
                            f"{', '.join(t.depends_on)}"))
                break

            self.parent.on_event("enjambre", f"ola {wave}: {len(listos)} en paralelo")

            with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                futs = {
                    pool.submit(self._one, t, session_id, share, results): t
                    for t in listos
                }
                for fut in as_completed(futs):
                    r = fut.result()
                    with self._lock:
                        results[r.name] = r

            for t in listos:
                pending.pop(t.name, None)

        return [results[t.name] for t in tasks]

    def _one(self, task: SubTask, session_id: str, share: float,
             done: dict[str, SubResult]) -> SubResult:
        sub_session = f"{session_id}/{task.name}"
        t0 = time.time()
        try:
            agent = self._spawn(sub_session, share)

            contexto = ""
            if task.depends_on:
                partes = [f"### {d}\n{done[d].text[:3000]}"
                          for d in task.depends_on if d in done]
                if partes:
                    contexto = ("Resultados de los que dependes:\n\n"
                                + "\n\n".join(partes) + "\n\n---\n\n")

            reply: AgentReply = agent.chat(
                contexto + task.instruction, sub_session,
                surface="subagente", capability=task.capability)

            return SubResult(
                name=task.name, ok=True, text=reply.text,
                tools_used=reply.tools_used, actions=reply.actions,
                cost_usd=reply.cost_usd,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Subagente %s falló", task.name)
            return SubResult(name=task.name, ok=False, text="", error=str(exc),
                             elapsed_ms=int((time.time() - t0) * 1000))

    # ------------------------------------------------------------------

    def decompose(self, goal: str, session_id: str, max_tasks: int = 5) -> list[SubTask]:
        """Deja que el modelo proponga el reparto, con dependencias."""
        from .agent import _parse_json
        from .contracts import Message

        c = self.parent.mesh.ask(
            Capability.REASONING,
            [Message("system", DECOMPOSE_SYSTEM.format(n=max_tasks)),
             Message("user", goal)],
            temperature=0.2, json_mode=True, max_tokens=1200)
        data = _parse_json(c.text) or {}

        out = []
        for item in (data.get("tasks") or [])[:max_tasks]:
            if not item.get("instruction"):
                continue
            out.append(SubTask(
                instruction=item["instruction"],
                name=item.get("name", ""),
                depends_on=item.get("depends_on", []) or [],
            ))
        return out or [SubTask(instruction=goal, name="sub-1")]

    def synthesize(self, goal: str, results: list[SubResult]) -> str:
        """Une los resultados. El padre integra; los hijos no se ven entre sí
        salvo por dependencia declarada."""
        from .contracts import Message

        ok = [r for r in results if r.ok]
        if not ok:
            fallos = "; ".join(f"{r.name}: {r.error}" for r in results)
            return f"Ningún subagente completó su parte. Errores: {fallos}"

        bloques = "\n\n".join(f"### {r.name}\n{r.text}" for r in ok)
        fallidos = [r for r in results if not r.ok]
        nota = ("\n\nNo completaron: "
                + ", ".join(f"{r.name} ({r.error[:80]})" for r in fallidos)
                if fallidos else "")

        c = self.parent.mesh.ask(
            Capability.REASONING,
            [Message("system",
                     "Integra estos resultados parciales en una respuesta única "
                     "y coherente para el usuario. No los enumeres ni repitas "
                     "sus encabezados: sintetiza. Si hay contradicciones entre "
                     "ellos, señálalas explícitamente en vez de promediar."),
             Message("user", f"Objetivo original: {goal}\n\n{bloques}{nota}")],
            temperature=0.3, max_tokens=4096)
        return c.text.strip()

    def solve(self, goal: str, session_id: str,
              max_tasks: int = 5) -> tuple[str, list[SubResult]]:
        """Ciclo completo: descomponer → paralelizar → sintetizar."""
        tasks = self.decompose(goal, session_id, max_tasks)
        self.parent.on_event("enjambre",
                             f"{len(tasks)} subtareas: {[t.name for t in tasks]}")
        results = self.run(tasks, session_id)
        return self.synthesize(goal, results), results


SUB_PERSONA = """Eres un subagente de Fibonacci con UNA tarea acotada.

- Haz exactamente lo que se te pide. No amplíes el alcance.
- Usa herramientas; no describas lo que harías.
- Devuelve el resultado, no un relato del proceso.
- Si tu tarea resulta imposible con lo que tienes, dilo en la primera línea
  con "BLOQUEADO:" y explica qué falta. No improvises un resultado plausible.
"""

DECOMPOSE_SYSTEM = """Divide el objetivo en subtareas PARALELIZABLES.

Reglas:
- Máximo {n} subtareas.
- Cada una debe poder ejecutarse sin ver el trabajo de las otras, salvo que
  declares la dependencia en `depends_on`.
- Si el objetivo es intrínsecamente secuencial, devuelve UNA sola tarea.
  Paralelizar algo secuencial produce trabajo duplicado y contradictorio.
- Nombres cortos en kebab-case.

JSON: {{"tasks":[{{"name":"...","instruction":"...","depends_on":[]}}]}}"""
