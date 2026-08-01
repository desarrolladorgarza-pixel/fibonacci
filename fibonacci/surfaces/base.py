"""
FIBONACCI — Superficies: el contrato.

Hermes trae Telegram, Discord, Slack, WhatsApp y Signal dentro del proceso del
gateway. Da mucho de entrada y también acopla el núcleo a cinco SDKs y sus
rupturas de API.

Aquí una superficie es un adaptador que implementa cuatro métodos. El núcleo
no sabe que existen. Añadir una plataforma nueva es un archivo, no un parche
al gateway.

El contrato deja la continuidad explícita: `session_key()` decide qué
conversaciones comparten memoria. Que tu chat de Telegram y tu terminal sean
la misma sesión es una decisión del adaptador, no un accidente.

## Por qué este módulo re-exporta en vez de definir

Hasta la 0.7.0 este archivo declaraba su propio `Inbound`, su propio `Outbound`
y su propio `Surface`, y `live.py` declaraba otros tres con el mismo nombre.
Nadie importaba estos: eran código muerto. Peor que muerto, **engañoso**: el
`Inbound` de aquí no tenía `display`, y `SurfaceRunner` lo usa. Quien
escribiera una superficie nueva siguiendo este contrato —que es exactamente lo
que el README invita a hacer— se encontraba con un `AttributeError` en cuanto
alguien le escribiera al bot.

Ahora hay una sola definición de cada cosa, la que el runtime usa de verdad.
Este módulo es la puerta documentada: importa de aquí y lo que escribas
funcionará.

    from fibonacci.surfaces.base import Inbound, Outbound, Surface

    class MiSuperficie(Surface):
        name = "mi-plataforma"

        def receive(self): ...
        def send(self, channel_id, out): ...
"""

from __future__ import annotations

from .live import Inbound, Outbound, Surface, SurfaceRunner

__all__ = ["Inbound", "Outbound", "Surface", "SurfaceRunner"]
