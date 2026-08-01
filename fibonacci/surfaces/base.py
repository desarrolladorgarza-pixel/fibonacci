"""
FIBONACCI — Superficies.

Hermes trae Telegram, Discord, Slack, WhatsApp y Signal dentro del proceso del
gateway. Da mucho de entrada y también acopla el núcleo a cinco SDKs y sus
rupturas de API.

Aquí una superficie es un adaptador que implementa cuatro métodos. El núcleo
no sabe que existen. Añadir una plataforma nueva es un archivo, no un parche
al gateway.

El contrato deja la continuidad explícita: `session_key()` decide qué
conversaciones comparten memoria. Que tu chat de Telegram y tu terminal sean
la misma sesión es una decisión del adaptador, no un accidente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Inbound:
    text: str
    user_id: str
    channel_id: str = ""
    attachments: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outbound:
    text: str
    files: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class Surface(Protocol):
    name: str

    def session_key(self, msg: Inbound) -> str:
        """Qué sesión corresponde. Devolver la misma clave en dos superficies
        distintas hace que compartan historial y memoria."""
        ...

    def authorized(self, msg: Inbound) -> bool:
        """Emparejamiento/allowlist. Sin esto, cualquiera con el enlace del bot
        habla con tu agente y toca tus archivos."""
        ...

    def receive(self): ...
    def send(self, channel_id: str, out: Outbound) -> None: ...
