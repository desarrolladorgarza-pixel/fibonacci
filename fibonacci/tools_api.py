"""
FIBONACCI — Herramientas de API y primitivas.

Conecta `api.py` y `primitives.py` al ToolBox para que el modelo pueda usarlos.

Dos decisiones de diseño que importan:

**El modelo nunca ve una credencial.** `api.call` recibe el *nombre* de la
credencial, no su valor. La sustitución ocurre en `ApiClient`, después de que
el modelo escribió la petición. Aunque una inyección logre volcar todo el
contexto, el token no está ahí.

**Las llamadas mutantes a APIs externas no fingen ser reversibles.** Un `POST`
a un servicio de terceros no tiene undo — el journal lo registra como
irreversible y pide confirmación. Salvo que registres un inverso explícito
(`api.register_undo`), que es lo correcto cuando la API sí ofrece la operación
contraria.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .api import ApiClient, Endpoint, OpenApiSpec, Vault
from .contracts import ToolSpec
from .tools import ToolBox

log = logging.getLogger("fibonacci.tools_api")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def attach_api(box: ToolBox, vault: Vault | None = None,
               client: ApiClient | None = None) -> ApiClient:
    """Cliente HTTP general y gestión de credenciales por nombre."""
    v = vault or Vault()
    c = client or ApiClient(v)

    def _call(method: str, url: str, headers: dict | None = None,
              body: Any = None, credential: str | None = None,
              params: dict | None = None) -> str:
        r = c.request(method, url, headers=headers, body=body,
                      credential=credential, params=params)
        return r.summarize(limit=box.max_result_chars)

    box.register(
        ToolSpec(
            "api.get",
            "Petición GET a una API. Usa `credential` con el NOMBRE de una "
            "credencial guardada (nunca escribas el token; no lo tienes).",
            {"type": "object",
             "properties": {
                 "url": {"type": "string"},
                 "credential": {"type": "string",
                                "description": "nombre en la bóveda"},
                 "params": {"type": "object"},
                 "headers": {"type": "object"}},
             "required": ["url"]}),
        lambda url, credential=None, params=None, headers=None:
            _call("GET", url, headers, None, credential, params),
    )

    box.register(
        ToolSpec(
            "api.call",
            "Petición HTTP con método arbitrario. Los métodos que escriben "
            "(POST/PUT/PATCH/DELETE) son IRREVERSIBLES en un servicio externo "
            "y piden confirmación.",
            {"type": "object",
             "properties": {
                 "method": {"type": "string",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                 "url": {"type": "string"},
                 "body": {"type": "object"},
                 "credential": {"type": "string"},
                 "headers": {"type": "object"},
                 "params": {"type": "object"}},
             "required": ["method", "url"]},
            mutating=True, reversible=False, danger=2),
        _call,
    )

    box.register(
        ToolSpec(
            "api.credentials",
            "Lista los NOMBRES de credenciales disponibles y a qué hosts "
            "están limitadas. Nunca devuelve los valores.",
            {"type": "object", "properties": {}}),
        lambda: (json.dumps(v.describe(), ensure_ascii=False, indent=2)
                 if not v.locked else
                 "La bóveda está bloqueada. Ejecuta `fib vault unlock`."),
    )

    return c


def attach_openapi(box: ToolBox, spec: OpenApiSpec, client: ApiClient,
                   prefix: str | None = None,
                   credential: str | None = None,
                   include_mutating: bool = True,
                   only: list[str] | None = None) -> int:
    """
    Convierte una spec OpenAPI en herramientas vivas. Devuelve cuántas registró.

        spec = OpenApiSpec.from_url("https://api.ejemplo.com/openapi.json")
        n = attach_openapi(box, spec, client, credential="ejemplo")
    """
    registradas = 0
    for tool_spec, ep in spec.to_tool_specs(
            prefix=prefix, include_mutating=include_mutating, only=only):
        box.register(tool_spec, _make_caller(client, spec.base_url, ep, credential))
        registradas += 1
    log.info("OpenAPI '%s': %d herramientas registradas", spec.title, registradas)
    return registradas


def _make_caller(client: ApiClient, base_url: str, ep: Endpoint,
                 credential: str | None) -> Callable[..., str]:
    """Cierra sobre el endpoint. Separa parámetros de ruta, query y cuerpo
    según lo que declare la spec, no adivinando."""

    def call(**kwargs) -> str:
        path = ep.path
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        body = kwargs.pop("body", None)

        for p in ep.parameters:
            if not isinstance(p, dict):
                continue
            nombre = p.get("name")
            if nombre not in kwargs:
                continue
            valor = kwargs.pop(nombre)
            donde = p.get("in", "query")
            if donde == "path":
                path = path.replace("{" + nombre + "}", str(valor))
            elif donde == "header":
                headers[nombre] = str(valor)
            else:
                query[nombre] = valor

        # Lo que sobre y siga en la ruta como placeholder, se intenta rellenar.
        for k in list(kwargs):
            if "{" + k + "}" in path:
                path = path.replace("{" + k + "}", str(kwargs.pop(k)))
        query.update({k: v for k, v in kwargs.items() if v is not None})

        if "{" in path:
            import re
            pendientes = re.findall(r"\{(\w+)\}", path)
            if pendientes:
                return (f"Faltan parámetros de ruta: {', '.join(pendientes)}. "
                        f"La ruta es {ep.path}")

        url = base_url.rstrip("/") + path
        r = client.request(ep.method, url, headers=headers, body=body,
                           credential=credential, params=query)
        return r.summarize()

    return call


def attach_primitives(box: ToolBox, agent=None) -> None:
    """
    Expone las primitivas al modelo como herramientas de control de flujo.

    No ejecutan código arbitrario: operan sobre *otras herramientas* ya
    registradas. `primitive.retry` reintenta una llamada a herramienta; no
    ejecuta lo que el modelo escriba.
    """
    from .primitives import Fallback, Observe, Race, Retry

    def _invoke(nombre: str, args: dict, session: str = "primitiva"):
        r = box.invoke(nombre, args or {}, session)
        if not r.ok:
            raise RuntimeError(r.content[:300])
        return r.content

    def retry(tool: str, arguments: dict | None = None, attempts: int = 3) -> str:
        out = Retry(attempts, base_delay=1.0).run(
            lambda: _invoke(tool, arguments or {}))
        cab = f"[retry {out.attempts}/{attempts}] "
        return cab + (str(out.value) if out.ok else f"FALLÓ: {out.error}")

    box.register(
        ToolSpec(
            "flow.retry",
            "Reintenta una herramienta con retroceso exponencial. Úsala cuando "
            "el fallo sea transitorio (red, servicio ocupado), NO cuando sea "
            "estructural: repetir algo imposible solo gasta tiempo.",
            {"type": "object",
             "properties": {"tool": {"type": "string"},
                            "arguments": {"type": "object"},
                            "attempts": {"type": "integer", "default": 3}},
             "required": ["tool"]}),
        retry,
    )

    def fallback(tools: list[dict]) -> str:
        if not tools:
            return "sin alternativas"
        primera = tools[0]
        resto = [(lambda t=t: _invoke(t["tool"], t.get("arguments", {})))
                 for t in tools[1:]]
        out = Fallback(resto).run(
            lambda: _invoke(primera["tool"], primera.get("arguments", {})))
        if out.ok:
            return f"[alternativa {out.attempts - 1}] {out.value}"
        return f"todas fallaron: {out.error}"

    box.register(
        ToolSpec(
            "flow.fallback",
            "Prueba varias herramientas en orden hasta que una funcione. Para "
            "cuando la primera opción puede no estar disponible y hay un "
            "camino alterno distinto (no el mismo repetido).",
            {"type": "object",
             "properties": {
                 "tools": {"type": "array",
                           "items": {"type": "object",
                                     "properties": {"tool": {"type": "string"},
                                                    "arguments": {"type": "object"}}}}},
             "required": ["tools"]}),
        fallback,
    )

    def race(tools: list[dict], timeout: int = 60) -> str:
        competidores = [(lambda t=t: _invoke(t["tool"], t.get("arguments", {})))
                        for t in tools]
        out = Race(competidores, timeout=timeout).run(None)
        return str(out.value) if out.ok else f"nadie respondió: {out.error}"

    box.register(
        ToolSpec(
            "flow.race",
            "Lanza varias herramientas en paralelo y devuelve la primera que "
            "responda. Útil para consultar varias fuentes a la vez.",
            {"type": "object",
             "properties": {"tools": {"type": "array", "items": {"type": "object"}},
                            "timeout": {"type": "integer", "default": 60}},
             "required": ["tools"]}),
        race,
    )

    def observe(tool: str, arguments: dict | None = None,
                expect: str = "", timeout: int = 120, interval: int = 5) -> str:
        def condicion() -> bool:
            try:
                salida = _invoke(tool, arguments or {})
                return expect.lower() in str(salida).lower() if expect else True
            except Exception:  # noqa: BLE001
                return False
        out = Observe(condicion, timeout=timeout, interval=interval,
                      description=f"'{expect}' en {tool}").run(None)
        return (f"cumplido tras {out.attempts} sondeo(s)" if out.ok
                else f"no se cumplió: {out.error}")

    box.register(
        ToolSpec(
            "flow.observe",
            "Espera a que una herramienta devuelva algo que contenga el texto "
            "esperado, sondeando con intervalo. Para trabajo asíncrono: un "
            "despliegue que tarda, un job que termina.",
            {"type": "object",
             "properties": {"tool": {"type": "string"},
                            "arguments": {"type": "object"},
                            "expect": {"type": "string"},
                            "timeout": {"type": "integer", "default": 120},
                            "interval": {"type": "integer", "default": 5}},
             "required": ["tool"]}),
        observe,
    )

    if agent is not None:
        def checkpoint(label: str) -> str:
            import time as _t
            agent._checkpoints = getattr(agent, "_checkpoints", {})
            agent._checkpoints[label] = _t.time()
            agent.journal.trace("primitiva", "checkpoint", label)
            return (f"punto '{label}' establecido. Para volver aquí: "
                    f"flow.rollback con label='{label}'")

        def rollback(label: str) -> str:
            marcas = getattr(agent, "_checkpoints", {})
            if label not in marcas:
                return f"no existe el punto '{label}'"
            from .contracts import ActionStatus
            from .journal import _row_to_action

            filas = agent.journal.store.all(
                "SELECT * FROM actions WHERE ts>=? AND status=? ORDER BY ts DESC",
                (marcas[label], ActionStatus.APPLIED.value))
            hechas, notas = 0, []
            for r in filas:
                ok, msg = agent.journal._undo(_row_to_action(r))
                notas.append(msg)
                hechas += ok
            return f"{hechas} acción(es) revertidas hasta '{label}':\n" + "\n".join(notas)

        box.register(
            ToolSpec("flow.checkpoint",
                     "Marca un punto de retorno antes de una secuencia "
                     "arriesgada. Después puedes revertir todo hasta aquí.",
                     {"type": "object",
                      "properties": {"label": {"type": "string"}},
                      "required": ["label"]}),
            checkpoint,
        )
        box.register(
            ToolSpec("flow.rollback",
                     "Revierte todas las acciones hechas desde un punto de "
                     "retorno. Úsalo cuando una secuencia salió mal.",
                     {"type": "object",
                      "properties": {"label": {"type": "string"}},
                      "required": ["label"]},
                     mutating=True, reversible=False, danger=1),
            rollback,
        )
