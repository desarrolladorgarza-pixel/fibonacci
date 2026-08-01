"""
FIBONACCI Mesh — Router.

Traduce capacidad -> modelo, con cascada de respaldo. Si el modelo local no
responde (Ollama reiniciándose, batería del teléfono, red caída), degrada al
siguiente candidato sin que el agente se entere.

Modos:
  local   — solo modelos en tu hardware. Si no hay candidato, FALLA.
            Es una decisión explícita, no una fuga silenciosa a la nube.
  hybrid  — prefiere local, nube como respaldo.
  cloud   — sin restricción.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from ..contracts import Capability, Completion, Message, ToolSpec
from .providers import Provider, ProviderError
from .registry import Catalog, ModelCard

log = logging.getLogger("fibonacci.mesh")


@dataclass
class Ledger:
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, card: ModelCard, c: Completion) -> None:
        with self._lock:
            self.cost_usd += card.cost_of(c.prompt_tokens, c.completion_tokens)
            self.prompt_tokens += c.prompt_tokens
            self.completion_tokens += c.completion_tokens
            self.calls += 1
            self.by_model[card.id] = self.by_model.get(card.id, 0) + 1


class CircuitBreaker:
    """Un modelo que falla 3 veces sale de rotación 90s. Sin esto, un servicio
    reiniciándose convierte cada turno en minutos de espera."""

    def __init__(self, threshold: int = 3, cooldown: float = 90.0):
        self.threshold, self.cooldown = threshold, cooldown
        self._fails: dict[str, int] = {}
        self._open: dict[str, float] = {}

    def is_open(self, mid: str) -> bool:
        t = self._open.get(mid)
        if t is None:
            return False
        if time.time() - t > self.cooldown:
            self._open.pop(mid, None)
            self._fails.pop(mid, None)
            return False
        return True

    def fail(self, mid: str) -> None:
        self._fails[mid] = self._fails.get(mid, 0) + 1
        if self._fails[mid] >= self.threshold:
            self._open[mid] = time.time()
            log.warning("Circuito abierto: %s", mid)

    def ok(self, mid: str) -> None:
        self._fails.pop(mid, None)


class ModelMesh:
    def __init__(self, catalog: Catalog, providers: dict[str, Provider],
                 mode: str = "hybrid", ledger: Ledger | None = None):
        if mode not in ("local", "hybrid", "cloud"):
            raise ValueError("mode debe ser local | hybrid | cloud")
        self.catalog = catalog
        self.providers = providers
        self.mode = mode
        self.ledger = ledger or Ledger()
        self.breaker = CircuitBreaker()

    def ask(self, capability: Capability, messages: list[Message], *,
            tools: list[ToolSpec] | None = None, temperature: float = 0.4,
            max_tokens: int = 4096, json_mode: bool = False,
            min_context: int = 0, exclude: set[str] | None = None) -> Completion:
        cands = self.catalog.find(
            capability, local_only=(self.mode == "local"),
            min_context=min_context, exclude=exclude,
        )
        if self.mode == "hybrid":
            cands.sort(key=lambda c: (not c.local, c.priority))
        if not cands:
            raise ProviderError(
                "mesh", f"Sin modelo para {capability.value} (modo={self.mode})", False)

        errors = []
        for card in cands:
            if self.breaker.is_open(card.id):
                continue
            prov = self.providers.get(card.provider)
            if prov is None:
                continue
            try:
                c = prov.complete(card.id, messages, tools=tools,
                                  temperature=temperature, max_tokens=max_tokens,
                                  json_mode=json_mode)
                c.cost_usd = card.cost_of(c.prompt_tokens, c.completion_tokens)
                self.ledger.record(card, c)
                self.breaker.ok(card.id)
                return c
            except ProviderError as e:
                self.breaker.fail(card.id)
                errors.append(f"{card.id}: {e}")
                log.warning("Degradando desde %s", card.id)

        raise ProviderError("mesh", "Cascada agotada: " + " | ".join(errors), False)

    def stream(self, capability: Capability, messages: list[Message], *,
               temperature: float = 0.4, max_tokens: int = 4096):
        """Transmite desde el primer candidato que soporte streaming. Si
        ninguno lo soporta, cae a `ask` y emite el texto completo de una vez."""
        cands = self.catalog.find(capability, local_only=(self.mode == "local"))
        if self.mode == "hybrid":
            cands.sort(key=lambda c: (not c.local, c.priority))
        for card in cands:
            if self.breaker.is_open(card.id):
                continue
            prov = self.providers.get(card.provider)
            if prov is None or not hasattr(prov, "stream"):
                continue
            try:
                yield from prov.stream(card.id, messages, temperature=temperature,
                                       max_tokens=max_tokens)
                self.breaker.ok(card.id)
                return
            except ProviderError:
                self.breaker.fail(card.id)
                continue
        # Sin streaming disponible: una sola emisión con la respuesta completa.
        yield self.ask(capability, messages, temperature=temperature,
                       max_tokens=max_tokens).text

    def embed(self, texts: list[str]) -> list[list[float]]:
        for card in self.catalog.find(Capability.EMBEDDING,
                                      local_only=(self.mode == "local")):
            prov = self.providers.get(card.provider)
            if prov is None:
                continue
            try:
                return prov.embed(card.id, texts)
            except ProviderError:
                continue
        raise ProviderError("mesh", "Sin embeddings disponibles", False)

    def diagnose(self) -> dict[str, bool]:
        return {n: p.health() for n, p in self.providers.items()}
