"""
FIBONACCI — El agente.

El bucle: percibir → responder → actuar → registrar → aprender.

Tres mejoras concretas sobre el bucle de Hermes:

**1. Presupuesto de contexto proactivo.** Hermes te da `/compress` cuando ya
te quedaste sin ventana. Aquí el contexto se arma con un presupuesto: se
reserva espacio para las herramientas y la respuesta, y lo que sobra se llena
por prioridad (turno actual > memoria relevante > historial reciente). Nunca
choca con el techo porque nunca lo intenta.

**2. Aprendizaje verificado.** Hermes crea skills tras tareas complejas. Aquí
la skill nace como *candidata*, se prueba en sombra y solo se activa si su
tasa de éxito lo justifica. Si empeora, se retira sola.

**3. Todo lo que muta pasa por el journal.** Con su inverso. Ver journal.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .contracts import (
    Capability, DurableTask, Message, Note, Skill, TaskState, Turn,
)
from .journal import Journal
from .memory import Memory
from .mesh.router import ModelMesh
from .platform import PLATFORM, describe
from .tools import ToolBox

log = logging.getLogger("fibonacci.agent")

MAX_TOOL_STEPS = 16

BASE_PERSONA = """Eres Fibonacci, el agente personal de quien te habla.

Cómo trabajas:
- Actúas en vez de explicar cómo se haría. Si tienes una herramienta, úsala.
- Antes de una acción destructiva o irreversible, dilo en una línea y espera.
- Si no sabes algo, lo dices. No inventas rutas, comandos ni resultados.
- Respondes en el idioma en que te hablan, con la brevedad de un colega
  competente: sin preámbulo, sin repetir la pregunta, sin relleno.

Lo que puedes deshacer, hazlo con confianza: tus cambios en archivos quedan
registrados y el usuario puede revertirlos con `fib undo`. Lo irreversible
(comandos de shell, envíos) requiere su confirmación explícita."""


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class SpendBudget:
    """
    Techo duro. En 0.1.0 no habia ninguno: un bucle de 16 pasos con modelo de
    nube podia gastar sin limite. En local el recurso escaso no es el dinero
    sino el reloj, por eso `max_seconds` tambien es un corte.
    """

    max_usd: float = 2.0
    max_tokens: int = 400_000
    max_seconds: float = 900.0
    spent_usd: float = 0.0
    spent_tokens: int = 0
    started: float = field(default_factory=time.time)

    def charge(self, usd: float, tokens: int) -> None:
        self.spent_usd += usd
        self.spent_tokens += tokens
        self.check()

    def check(self) -> None:
        if self.spent_usd > self.max_usd:
            raise BudgetExceeded(f"gasto ${self.spent_usd:.4f} supera el limite "
                                 f"de ${self.max_usd:.2f}")
        if self.spent_tokens > self.max_tokens:
            raise BudgetExceeded(f"{self.spent_tokens} tokens superan el limite "
                                 f"de {self.max_tokens}")
        if time.time() - self.started > self.max_seconds:
            raise BudgetExceeded(f"tiempo excedido ({self.max_seconds:.0f}s)")

    def reset(self) -> None:
        self.spent_usd = 0.0
        self.spent_tokens = 0
        self.started = time.time()

    @property
    def report(self) -> dict:
        return {"usd": round(self.spent_usd, 5), "tokens": self.spent_tokens,
                "segundos": round(time.time() - self.started, 1)}


@dataclass
class ContextBudget:
    """
    Reparto explícito de la ventana. Los porcentajes no son mágicos: reflejan
    que el turno actual siempre importa más que el historial, y que quedarse
    sin espacio para la respuesta es el peor de los fallos.
    """

    total: int = 32_768
    reserve_output: float = 0.25
    share_system: float = 0.10
    share_memory: float = 0.15
    share_history: float = 0.35

    @property
    def usable(self) -> int:
        return int(self.total * (1 - self.reserve_output))

    def slice(self, share: float) -> int:
        return int(self.usable * share)


def _approx_tokens(text: str) -> int:
    """~3.6 chars/token: sirve para español e inglés sin cargar un tokenizer."""
    return max(1, len(text) // 4 + len(text) // 16)


@dataclass
class AgentReply:
    text: str
    tools_used: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    model: str = ""
    redacted: list[str] = field(default_factory=list)
    truncated: bool = False
    injection_flags: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, mesh: ModelMesh, memory: Memory, journal: Journal,
                 tools: ToolBox, persona: str = BASE_PERSONA,
                 on_event: Callable[[str, str], None] | None = None,
                 budget: SpendBudget | None = None):
        self.mesh = mesh
        self.memory = memory
        self.journal = journal
        self.tools = tools
        self.persona = persona
        self.on_event = on_event or (lambda kind, payload: None)
        self.budget = budget or SpendBudget()
        # Skills usadas en el turno anterior, esperando veredicto. El veredicto
        # llega del turno SIGUIENTE: si el usuario deshizo, repitio o corrigio,
        # fue fallo. Sin esta ventana, `score_skill` era codigo muerto.
        self._pending: dict[str, list[str]] = {}
        self._last_user: dict[str, str] = {}

    # ------------------------------------------------------------------

    def chat(self, user_text: str, session_id: str, *,
             surface: str = "cli", capability: Capability | None = None) -> AgentReply:
        t0 = time.time()

        # 1. Veredicto de las skills del turno anterior, ANTES de nada mas.
        self._settle_pending(session_id, user_text)

        # 2. La contaminacion es por turno: el riesgo es la cadena dentro de
        #    una ejecucion, no la historia completa.
        self.tools.taint.reset()
        self.budget.reset()

        cap = capability or self._route(user_text)
        ctx = ContextBudget(total=self._window(cap))
        # El truncado de resultados se deriva del presupuesto real, no de una
        # constante suelta. En 0.1.0 los tool results lo esquivaban por completo.
        self.tools.max_result_chars = max(4_000, ctx.slice(0.20) * 4)

        skills = self.memory.select_skills(user_text)
        messages = self._build_context(user_text, session_id, ctx, skills)
        specs = self.tools.specs()

        self.journal.trace(session_id, "turno",
                           f"capacidad={cap.value} ventana={ctx.total} "
                           f"skills={[s.name for s in skills]}")

        used, actions, redacted, truncated = [], [], [], False
        try:
            c = self.mesh.ask(cap, messages, tools=specs, max_tokens=4096)
            self.budget.charge(c.cost_usd, c.prompt_tokens + c.completion_tokens)

            steps = 0
            while c.tool_calls and steps < MAX_TOOL_STEPS:
                steps += 1
                messages.append(Message("assistant", c.text or ""))
                for call in c.tool_calls:
                    self.on_event("tool", f"{call.name}({_brief(call.arguments)})")
                    r = self.tools.invoke(call.name, call.arguments, session_id)
                    used.append(call.name)
                    if r.action_id:
                        actions.append(r.action_id)
                    if r.redacted:
                        redacted.extend(r.redacted)
                        self.on_event("redaccion",
                                      f"{len(r.redacted)} secreto(s) ocultados")
                    if r.blocked_reason:
                        self.on_event("bloqueo", r.blocked_reason[:120])
                    messages.append(Message("tool", r.content, name=call.name,
                                            tool_call_id=call.id))
                c = self.mesh.ask(cap, messages, tools=specs, max_tokens=4096)
                self.budget.charge(c.cost_usd, c.prompt_tokens + c.completion_tokens)

            if steps >= MAX_TOOL_STEPS:
                truncated = True
                log.warning("Tope de herramientas alcanzado en %s", session_id)
                self.journal.trace(session_id, "tope_herramientas", str(MAX_TOOL_STEPS))

            text = c.text.strip()
            model = c.model
        except BudgetExceeded as exc:
            self.journal.trace(session_id, "presupuesto", str(exc))
            self.on_event("bloqueo", f"presupuesto: {exc}")
            text = (f"Me detuve a medio camino: {exc}.\n\n"
                    f"Alcancé a ejecutar {len(used)} herramienta(s). "
                    "Ajusta el límite o divide la tarea.")
            model = ""

        reply = AgentReply(
            text=text, tools_used=used, actions=actions,
            tokens=self.budget.spent_tokens, cost_usd=self.budget.spent_usd,
            elapsed_ms=int((time.time() - t0) * 1000), model=model,
            redacted=sorted(set(redacted)), truncated=truncated,
            injection_flags=list(self.tools.taint.injection_flags),
        )

        if reply.injection_flags:
            self.on_event("inyeccion",
                          f"{len(reply.injection_flags)} señal(es) en contenido externo")

        self.memory.add_turn(Turn(
            session_id=session_id, user=user_text, assistant=reply.text,
            tools_used=used, tokens=reply.tokens, surface=surface,
        ))

        # 3. Las skills usadas quedan pendientes de veredicto para el turno
        #    siguiente. Aqui es donde `score_skill` deja de ser codigo muerto.
        if skills:
            self._pending[session_id] = [sk.name for sk in skills]
        self._last_user[session_id] = user_text

        self._maybe_learn(user_text, reply, session_id)
        return reply

    def chat_stream(self, user_text: str, session_id: str, *, surface: str = "cli"):
        """
        Versión en streaming para consultas conversacionales. Emite fragmentos
        conforme llegan. Si la intención requiere herramientas, NO transmite:
        cede al `chat` normal, porque el valor del streaming es la conversación
        fluida, no ver acciones a medio ejecutar.
        """
        self._settle_pending(session_id, user_text)
        cap = self._route(user_text)

        # Herramientas o tarea compleja -> camino normal, sin streaming.
        if cap in (Capability.CODE, Capability.REASONING) or \
                any(v in user_text.lower() for v in
                    ("crea", "borra", "mueve", "ejecuta", "instala", "abre")):
            reply = self.chat(user_text, session_id, surface=surface)
            yield reply.text
            return

        ctx = ContextBudget(total=self._window(cap))
        messages = self._build_context(user_text, session_id, ctx,
                                       self.memory.select_skills(user_text))
        partes = []
        try:
            for frag in self.mesh.stream(cap, messages, max_tokens=4096):
                partes.append(frag)
                yield frag
        except Exception as exc:  # noqa: BLE001
            log.warning("Streaming falló, cae a modo normal: %s", exc)
            if not partes:
                yield self.chat(user_text, session_id, surface=surface).text
                return

        texto = "".join(partes)
        self.memory.add_turn(Turn(session_id=session_id, user=user_text,
                                  assistant=texto, surface=surface))

    # ------------------------------------------------------------------

    def _settle_pending(self, session_id: str, user_text: str) -> None:
        """
        Cierra el ciclo de aprendizaje. La señal es implicita y honesta:
        si el usuario deshizo, repitio la instruccion o corrigio, la skill
        fallo. Si siguio adelante, funciono.
        """
        names = self._pending.pop(session_id, [])
        if not names:
            return

        low = user_text.lower().strip()
        prev = self._last_user.get(session_id, "")

        deshizo = any(k in low for k in
                      ("/undo", "deshaz", "revierte", "undo", "reviértelo"))
        corrigio = any(k in low for k in
                       ("no,", "mal", "está mal", "esta mal", "no era", "equivocado",
                        "otra vez", "de nuevo", "no funciona", "incorrecto",
                        "no es lo que", "te equivocaste"))
        repitio = bool(prev) and _similar(prev, user_text) > 0.72

        won = not (deshizo or corrigio or repitio)
        motivo = ("deshizo" if deshizo else "corrigio" if corrigio
                  else "repitio" if repitio else "continuo")

        for name in names:
            estado = self.memory.score_skill(name, won)
            self.journal.trace(session_id, "veredicto_skill",
                               f"{name}: {'acierto' if won else 'fallo'} "
                               f"({motivo}) -> {estado}")
            if estado == "retired":
                self.on_event("skill", f"'{name}' retirada por bajo desempeño")
            elif estado == "active":
                self.on_event("skill", f"'{name}' promovida a activa")

    def settle_undo(self, session_id: str) -> None:
        """El CLI llama esto tras un `undo` explicito: es la señal negativa
        mas clara que existe y no debe esperar al turno siguiente."""
        for name in self._pending.pop(session_id, []):
            estado = self.memory.score_skill(name, False)
            self.journal.trace(session_id, "veredicto_skill",
                               f"{name}: fallo (undo explicito) -> {estado}")

    # ------------------------------------------------------------------

    def _route(self, text: str) -> Capability:
        """Enrutamiento por intención. Barato y acertado la mayoría de veces;
        cuando falla, el modelo elegido igual responde bien."""
        low = text.lower()
        if re.search(r"\b(código|code|función|bug|refactor|script|compil|test)\b", low):
            return Capability.CODE
        if len(text) > 4000:
            return Capability.LONG_CONTEXT
        if re.search(r"\b(plan|analiza|compara|diseña|investiga|estrategia|por qué)\b", low):
            return Capability.REASONING
        return Capability.CHAT

    def _window(self, cap: Capability) -> int:
        cands = self.mesh.catalog.find(cap, local_only=(self.mesh.mode == "local"))
        return cands[0].context_window if cands else 32_768

    def _build_context(self, user_text: str, session_id: str,
                       b: ContextBudget, skills=None) -> list[Message]:
        # 1. Sistema: persona + plataforma + skills aplicables
        skills = skills if skills is not None else self.memory.select_skills(user_text)
        sys_parts = [self.persona, f"\nEntorno: {describe()}, shell {PLATFORM.shell}."]
        if skills:
            sys_parts.append("\n# Procedimientos aprendidos\n" + "\n\n".join(
                f"## {s.name} ({s.status}, {s.wins}/{s.trials})\n{s.body}"
                for s in skills))
        system = _clip("\n".join(sys_parts), b.slice(b.share_system))

        # 2. Memoria relevante, con confianza decaída visible
        notes = self.memory.recall(user_text, k=8)
        if notes:
            mem = "\n".join(
                f"- {n.content}"
                + (f" (confianza {n.current_confidence():.0%})"
                   if n.current_confidence() < 0.6 else "")
                for n in notes)
            system += _clip("\n\n# Lo que sé de esta persona\n" + mem,
                            b.slice(b.share_memory))

        # 3. Contradicciones abiertas: se muestran, no se ocultan
        conflicts = self.memory.open_conflicts()
        if conflicts:
            system += "\n\n# Datos en conflicto (pregunta si es relevante)\n" + "\n".join(
                f'- "{a.content}" vs "{b_.content}"' for a, b_ in conflicts[:3])

        msgs = [Message("system", system)]

        # 4. Historial reciente, recortado por presupuesto
        room = b.slice(b.share_history)
        recent, spent = [], 0
        for t in reversed(self.memory.session(session_id, limit=40)):
            cost = _approx_tokens(t.user) + _approx_tokens(t.assistant)
            if spent + cost > room:
                break
            recent.append(t)
            spent += cost
        for t in reversed(recent):
            msgs.append(Message("user", t.user))
            if t.assistant:
                msgs.append(Message("assistant", t.assistant))

        msgs.append(Message("user", user_text))
        return msgs

    # ------------------------------------------------------------------

    def _maybe_learn(self, user_text: str, reply: AgentReply, session_id: str) -> None:
        """
        Extrae notas y skills candidatas. Discreto a propósito: solo cuando la
        interacción fue sustantiva. Un agente que aprende de cada "gracias"
        acumula ruido que después contamina cada respuesta.
        """
        if len(reply.tools_used) < 2 and len(user_text) < 120:
            return
        try:
            c = self.mesh.ask(
                Capability.EXTRACTION,
                [Message("user", _LEARN_PROMPT.format(
                    user=user_text[:3000], assistant=reply.text[:3000],
                    tools=", ".join(reply.tools_used) or "ninguna"))],
                temperature=0.1, json_mode=True, max_tokens=900,
            )
            data = _parse_json(c.text) or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("Aprendizaje omitido: %s", exc)
            return

        for item in (data.get("notes") or [])[:3]:
            content = (item.get("content") or "").strip()
            if len(content) < 8:
                continue
            note = Note(
                content=content, kind=item.get("kind", "fact"),
                confidence=float(item.get("confidence", 0.6)),
                half_life_days=float(item.get("half_life_days", 180)),
                source=f"sesión {session_id}",
            )
            _, conflicts = self.memory.remember(note)
            if conflicts:
                self.on_event("conflict",
                              f"Contradice: {conflicts[0].content[:80]}")

        proc = data.get("skill")
        if proc and proc.get("name") and len(reply.tools_used) >= 2:
            existing = {s.name for s in self.memory.skills()}
            if proc["name"] not in existing:
                self.memory.save_skill(Skill(
                    name=proc["name"], body=proc.get("body", ""),
                    description=proc.get("description", ""),
                    triggers=proc.get("triggers", []),
                    status="candidate",     # nace sin poder tocar un prompt real
                ))
                self.on_event("skill", f"Nueva candidata: {proc['name']}")

    # ------------------------------------------------------------------
    # Tareas durables
    # ------------------------------------------------------------------

    def plan(self, goal: str, session_id: str) -> DurableTask:
        c = self.mesh.ask(
            Capability.REASONING,
            [Message("system",
                     "Descompón el objetivo en pasos concretos y ejecutables. "
                     "Cada paso debe poder verificarse. Máximo 8. "
                     'Responde JSON: {"steps":["...","..."]}'),
             Message("user", goal)],
            temperature=0.2, json_mode=True, max_tokens=1200,
        )
        data = _parse_json(c.text) or {}
        from .contracts import Step

        steps = [Step(description=s) for s in (data.get("steps") or [goal])[:8]]
        return DurableTask(goal=goal, session_id=session_id, steps=steps)

    def advance(self, task: DurableTask) -> Iterator[DurableTask]:
        """
        Un paso por iteración, guardando estado. El llamador persiste entre
        pasos, así que apagar el proceso a media tarea no pierde el avance.

        **Exactamente un evento por paso.** Antes había además un `yield` final
        que repetía el último cursor solo para anunciar el estado DONE: quien
        persistía en cada iteración guardaba dos veces la misma posición y
        quien contaba eventos veía un paso de más. Ahora el último paso ya sale
        con `state=DONE` y `result` puesto.
        """
        task.state = TaskState.RUNNING

        if not task.steps:
            task.state = TaskState.DONE
            task.updated_at = time.time()
            yield task
            return

        while task.cursor < len(task.steps):
            step = task.steps[task.cursor]
            step.state = TaskState.RUNNING
            self.on_event("step", f"{task.cursor + 1}/{len(task.steps)}: {step.description}")
            try:
                r = self.chat(
                    f"Objetivo general: {task.goal}\n\nPaso actual: {step.description}\n\n"
                    "Ejecútalo ahora con las herramientas disponibles.",
                    task.session_id, surface=task.surface,
                )
                step.output, step.state = r.text, TaskState.DONE
            except Exception as exc:  # noqa: BLE001
                step.output, step.state = str(exc), TaskState.FAILED
                task.state = TaskState.FAILED
                task.updated_at = time.time()
                yield task
                return
            task.cursor += 1
            if task.cursor >= len(task.steps):
                task.state = TaskState.DONE
                task.result = "\n\n".join(
                    f"### {s.description}\n{s.output}"
                    for s in task.steps if s.output)
            task.updated_at = time.time()
            yield task


_LEARN_PROMPT = """Analiza esta interacción y extrae SOLO lo duradero.

Usuario: {user}
Agente: {assistant}
Herramientas usadas: {tools}

Devuelve JSON:
{{"notes":[{{"content":"dato sobre la persona o su entorno","kind":"fact|preference|project|person","confidence":0.0-1.0,"half_life_days":30-3650}}],
  "skill":{{"name":"kebab-case","description":"","triggers":["..."],"body":"procedimiento paso a paso"}}}}

Reglas:
- `notes` solo si es duradero. Nada efímero ("preguntó la hora").
- `half_life_days`: nombre o fecha de nacimiento = 3650; empleo actual = 365;
  en qué está trabajando esta semana = 30.
- `skill` SOLO si se resolvió un procedimiento repetible de varios pasos.
  Si no, omítelo. Prefiere no proponer nada a proponer algo genérico.
"""


def _clip(text: str, max_tokens: int) -> str:
    limit = max_tokens * 4
    return text if len(text) <= limit else text[:limit] + "\n[...recortado]"


def _brief(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s[:70] + ("…" if len(s) > 70 else "")


def _similar(a: str, b: str) -> float:
    """Jaccard sobre palabras. Suficiente para detectar 'me lo repitio casi
    igual', que es la senal de que la respuesta anterior no sirvio."""
    import difflib

    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    for cand in (text, *re.findall(r"\{.*\}", text, re.DOTALL)):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            continue
    return None
