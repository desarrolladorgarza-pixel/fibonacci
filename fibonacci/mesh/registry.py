"""
FIBONACCI Mesh — Catálogo.

El código pide `Capability`, nunca un nombre de modelo. Hermes te hace elegir
con `hermes model`; aquí el router elige y degrada solo si el elegido no
responde. Cambiar de proveedor es editar este archivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import Capability


@dataclass
class ModelCard:
    id: str
    provider: str
    capabilities: set[Capability]
    context_window: int = 32_768
    cost_in: float = 0.0
    cost_out: float = 0.0
    local: bool = False
    priority: int = 50
    notes: str = ""

    def cost_of(self, pin: int, pout: int) -> float:
        return (pin * self.cost_in + pout * self.cost_out) / 1_000_000


C = Capability

LOCAL = [
    ModelCard("qwen3:8b", "ollama", {C.CHAT, C.REASONING, C.EXTRACTION},
              32_768, local=True, priority=10,
              notes="Default sensato: corre hasta en 16 GB de RAM."),
    ModelCard("qwen2.5-coder:14b", "ollama", {C.CODE, C.EXTRACTION},
              32_768, local=True, priority=15),
    ModelCard("gpt-oss:120b", "ollama",
              {C.REASONING, C.LONG_CONTEXT, C.CRITIQUE, C.EXTRACTION},
              131_072, local=True, priority=20,
              notes="MoE ~5B activos. Para maquinas con 128 GB (DGX, Mac Studio)."),
    ModelCard("deepseek-r1:70b", "ollama", {C.CRITIQUE, C.REASONING},
              65_536, local=True, priority=40,
              notes="Otra familia: revisor decorrelacionado."),
    ModelCard("qwen2.5-vl:7b", "ollama", {C.VISION, C.EXTRACTION},
              32_768, local=True, priority=15),
    ModelCard("bge-m3", "ollama", {C.EMBEDDING}, 8_192, local=True, priority=5),
]

CLOUD = [
    ModelCard("claude-sonnet-4-6", "anthropic",
              {C.CHAT, C.REASONING, C.CODE, C.LONG_CONTEXT, C.VISION,
               C.EXTRACTION, C.CRITIQUE},
              200_000, 3.0, 15.0, priority=25),
    ModelCard("deepseek-chat", "openai_compat",
              {C.CHAT, C.REASONING, C.CODE, C.EXTRACTION},
              128_000, 0.27, 1.10, priority=30),
    ModelCard("openai/gpt-4o-mini", "openrouter",
              {C.CHAT, C.EXTRACTION, C.VISION}, 128_000, 0.15, 0.60, priority=35),
]

PROFILES: dict[str, list[ModelCard]] = {
    "local": LOCAL,
    "hybrid": LOCAL + CLOUD,
    "cloud": CLOUD,
}


@dataclass
class Catalog:
    cards: list[ModelCard] = field(default_factory=list)

    @classmethod
    def from_profile(cls, name: str) -> "Catalog":
        if name not in PROFILES:
            raise ValueError(f"Perfil '{name}' desconocido: {list(PROFILES)}")
        return cls(cards=list(PROFILES[name]))

    def find(self, cap: Capability, *, local_only: bool = False,
             min_context: int = 0, exclude: set[str] | None = None) -> list[ModelCard]:
        ex = exclude or set()
        out = [c for c in self.cards
               if cap in c.capabilities and c.id not in ex
               and c.context_window >= min_context
               and (c.local or not local_only)]
        return sorted(out, key=lambda c: (c.priority, c.cost_in))
