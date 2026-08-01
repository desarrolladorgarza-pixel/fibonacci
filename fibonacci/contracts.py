"""
FIBONACCI — Contratos base.

Un agente personal de propósito general. Todo el sistema habla estos tipos.
Sin dependencias externas: corre igual en un DGX, un VPS de $5, un Mac o un
teléfono con Termux.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------

class Capability(str, Enum):
    """
    Se pide capacidad, no modelo. Hermes te hace elegir con `hermes model`;
    Fibonacci elige por ti y degrada solo si el elegido no responde.
    """

    CHAT = "chat"                    # conversación general, baja latencia
    REASONING = "reasoning"          # planeación, tareas de varios pasos
    CODE = "code"
    EXTRACTION = "extraction"        # salida estructurada, parseo
    LONG_CONTEXT = "long_context"
    VISION = "vision"
    EMBEDDING = "embedding"
    CRITIQUE = "critique"            # revisión; debe ser otra familia
    TRANSCRIBE = "transcribe"        # audio -> texto


Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    images: list[str] = field(default_factory=list)   # rutas o base64


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    mutating: bool = False           # ¿altera el mundo? -> journal + inverso
    reversible: bool = True          # ¿puede deshacerse?
    danger: int = 0                  # 0 seguro, 1 sensible, 2 destructivo


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Acciones y reversibilidad — el diferenciador del producto
# --------------------------------------------------------------------------

class ActionStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    UNDONE = "undone"
    FAILED = "failed"
    IRREVERSIBLE = "irreversible"    # se aplicó y no hay vuelta atrás


@dataclass
class Action:
    """
    Toda mutación del mundo se registra como Action con su inverso.
    Esto es lo que permite `fib undo`: un agente al que puedes revertir
    es un agente al que puedes dejar trabajar solo.
    """

    tool: str
    arguments: dict[str, Any]
    session_id: str
    inverse_tool: str | None = None
    inverse_arguments: dict[str, Any] = field(default_factory=dict)
    snapshot: str | None = None          # estado previo, si aplica
    status: ActionStatus = ActionStatus.PENDING
    result: str = ""
    id: str = field(default_factory=lambda: _id("act"))
    ts: float = field(default_factory=time.time)

    @property
    def undoable(self) -> bool:
        return self.status == ActionStatus.APPLIED and (
            self.inverse_tool is not None or self.snapshot is not None
        )


# --------------------------------------------------------------------------
# Tareas durables
# --------------------------------------------------------------------------

class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Step:
    description: str
    state: TaskState = TaskState.QUEUED
    output: str = ""
    depends_on: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _id("step"))


@dataclass
class DurableTask:
    """
    El trabajo sobrevive al proceso, a la terminal y al dispositivo.
    Cierras la laptop, abres el teléfono, sigue donde iba.
    """

    goal: str
    session_id: str
    steps: list[Step] = field(default_factory=list)
    state: TaskState = TaskState.QUEUED
    cursor: int = 0
    surface: str = "cli"
    result: str = ""
    id: str = field(default_factory=lambda: _id("task"))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def progress(self) -> tuple[int, int]:
        done = sum(1 for s in self.steps if s.state == TaskState.DONE)
        return done, len(self.steps)


# --------------------------------------------------------------------------
# Memoria
# --------------------------------------------------------------------------

@dataclass
class Note:
    """
    Un dato sobre el usuario o su mundo. Con decaimiento: lo que aprendí de ti
    hace ocho meses vale menos que lo de ayer, y el sistema lo sabe.
    """

    content: str
    kind: str = "fact"               # fact | preference | project | person
    source: str = "conversation"
    confidence: float = 0.7
    half_life_days: float = 180.0    # 0 = no decae (p.ej. tu nombre)
    supersedes: str | None = None
    embedding: list[float] = field(default_factory=list)
    id: str = field(default_factory=lambda: _id("note"))
    ts: float = field(default_factory=time.time)

    def current_confidence(self, now: float | None = None) -> float:
        if self.half_life_days <= 0:
            return self.confidence
        age_days = ((now or time.time()) - self.ts) / 86400.0
        return self.confidence * (0.5 ** (age_days / self.half_life_days))

    @property
    def stale(self) -> bool:
        return self.current_confidence() < 0.25


@dataclass
class Skill:
    """Procedimiento aprendido. No se activa hasta ganarse la promoción."""

    name: str
    body: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    status: str = "candidate"        # candidate | shadow | active | retired
    trials: int = 0
    wins: int = 0
    version: str = "0.1"
    source: str = "learned"          # learned | authored | imported
    id: str = field(default_factory=lambda: _id("skl"))

    @property
    def win_rate(self) -> float:
        return self.wins / self.trials if self.trials else 0.0


@dataclass
class Turn:
    session_id: str
    user: str
    assistant: str = ""
    tools_used: list[str] = field(default_factory=list)
    tokens: int = 0
    surface: str = "cli"
    id: str = field(default_factory=lambda: _id("turn"))
    ts: float = field(default_factory=time.time)
