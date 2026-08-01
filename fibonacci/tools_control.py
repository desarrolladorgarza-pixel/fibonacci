"""
FIBONACCI — Herramientas de control de equipo.

Registra pantalla, entrada y remoto en el ToolBox, con el modelo de
autorización de identity.py. Va aparte del tools.py base porque son
capacidades opcionales: un Fibonacci en un servidor sin GUI no las necesita
y no debe pagar su costo de arranque.

Diseño clave: **cada acción de GUI captura antes y después**. No se puede
deshacer un clic, pero sí se puede demostrar exactamente qué pasó. Cuando la
reversibilidad es imposible, la trazabilidad es el sustituto honesto.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .contracts import ToolSpec
from .control import (
    Remote, RemoteError, ScreenError, _q, active_window, capture, click,
    input_backend, press_key, scroll, type_text, window_is_sensitive,
)
from .identity import Authority, Decision, Principal
from .tools import ToolBox

log = logging.getLogger("fibonacci.tools_control")


def attach_screen(box: ToolBox, authority: Authority | None = None,
                  principal: Principal | None = None) -> None:
    """Visión y control de la pantalla local."""

    def _guard(action: str) -> tuple[bool, str]:
        """Ventanas sensibles: confirmación obligatoria, sin excepción."""
        title = active_window()
        motivo = window_is_sensitive(title)
        if motivo:
            desc = (f"La ventana activa es '{title[:60]}', que coincide con un "
                    f"contexto sensible. Vas a ejecutar {action} ahí.")
            if box.confirm is None or not box.confirm(desc, 2):
                return False, f"cancelado: ventana sensible ({title[:40]})"
        if authority and principal:
            d, why = authority.check(f"screen.{action}", title)
            if d == Decision.DENY:
                return False, why
            if d == Decision.CONFIRM and (
                    box.confirm is None or not box.confirm(f"{action}: {why}", 1)):
                return False, f"cancelado: {why}"
        return True, ""

    box.register(
        ToolSpec("screen.capture",
                 "Toma una captura de la pantalla y devuelve la ruta y la "
                 "ventana activa. Úsala ANTES de cualquier clic para saber "
                 "dónde estás.",
                 {"type": "object", "properties": {}}),
        lambda: _shot_summary(),
    )

    def _click(x: int, y: int, button: str = "left", double: bool = False) -> str:
        ok, why = _guard("click")
        if not ok:
            return why
        before = _safe_capture()
        res = click(int(x), int(y), button, bool(double))
        after = _safe_capture()
        return (f"{res}\n[forense: antes={before} despues={after} "
                f"ventana='{active_window()[:60]}']")

    box.register(
        ToolSpec("screen.click",
                 "Clic en coordenadas. IRREVERSIBLE: no existe un des-clic. "
                 "Se guardan capturas antes y después como registro forense.",
                 {"type": "object",
                  "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                 "button": {"type": "string",
                                            "enum": ["left", "right", "middle"]},
                                 "double": {"type": "boolean"}},
                  "required": ["x", "y"]},
                 mutating=True, reversible=False, danger=2),
        _click,
    )

    def _type(text: str) -> str:
        ok, why = _guard("type")
        if not ok:
            return why
        before = _safe_capture()
        res = type_text(text)
        return f"{res}\n[forense: antes={before} ventana='{active_window()[:60]}']"

    box.register(
        ToolSpec("screen.type",
                 "Escribe texto en la ventana activa. IRREVERSIBLE.",
                 {"type": "object", "properties": {"text": {"type": "string"}},
                  "required": ["text"]},
                 mutating=True, reversible=False, danger=2),
        _type,
    )

    def _key(key: str) -> str:
        ok, why = _guard("key")
        return press_key(key) if ok else why

    box.register(
        ToolSpec("screen.key",
                 "Pulsa una tecla o combinación: 'Return', 'ctrl+s', 'cmd+q'. "
                 "IRREVERSIBLE.",
                 {"type": "object", "properties": {"key": {"type": "string"}},
                  "required": ["key"]},
                 mutating=True, reversible=False, danger=2),
        _key,
    )

    box.register(
        ToolSpec("screen.scroll", "Desplaza la ventana activa.",
                 {"type": "object", "properties": {"amount": {"type": "integer"}},
                  "required": ["amount"]},
                 mutating=True, reversible=False, danger=1),
        lambda amount: scroll(int(amount)),
    )


def _safe_capture() -> str:
    try:
        return capture().path.name
    except ScreenError as exc:
        return f"(sin captura: {exc})"


def _shot_summary() -> str:
    try:
        s = capture()
    except ScreenError as exc:
        return f"ERROR: {exc}"
    aviso = ""
    motivo = window_is_sensitive(s.window_title)
    if motivo:
        aviso = ("\n[ATENCION: ventana sensible. Cualquier accion aqui exigira "
                 "confirmacion explicita del usuario.]")
    return (f"captura: {s.path}\nventana activa: '{s.window_title}'\n"
            f"backend de entrada: {input_backend()}{aviso}")


# ---------------------------------------------------------------------------

def attach_remote(box: ToolBox, remote: Remote,
                  authority: Authority | None = None,
                  principal: Principal | None = None) -> None:
    """Control de servidores. Los archivos remotos SI son reversibles."""

    def _check(alias: str, action: str) -> tuple[bool, str]:
        if authority and principal:
            d, why = authority.check(f"remote.{action}", alias)
            if d == Decision.DENY:
                return False, why
            if d == Decision.CONFIRM and (
                    box.confirm is None or not box.confirm(
                        f"{action} en '{alias}': {why}", 2)):
                return False, f"cancelado: {why}"
        h = remote.get(alias)
        if h.scope == "confirm" and box.confirm is not None:
            if not box.confirm(f"'{alias}' ({h.host}) exige confirmacion "
                               f"para {action}", 2):
                return False, f"cancelado por el usuario en '{alias}'"
        return True, ""

    box.register(
        ToolSpec("remote.hosts", "Lista los servidores registrados y su ambito.",
                 {"type": "object", "properties": {}}),
        lambda: "\n".join(
            f"{h.alias:16s} {h.target:30s} ambito={h.scope}"
            + (f"  ({h.note})" if h.note else "")
            for h in remote.hosts.values()) or "sin servidores registrados",
    )

    box.register(
        ToolSpec("remote.probe",
                 "Reconocimiento de un servidor: SO, carga, disco y servicios.",
                 {"type": "object", "properties": {"alias": {"type": "string"}},
                  "required": ["alias"]}),
        lambda alias: _fmt_probe(remote.probe(alias)),
    )

    box.register(
        ToolSpec("remote.read", "Lee un archivo de un servidor.",
                 {"type": "object",
                  "properties": {"alias": {"type": "string"},
                                 "path": {"type": "string"}},
                  "required": ["alias", "path"]}),
        lambda alias, path: remote.read(alias, path),
    )

    def _run(alias: str, command: str) -> str:
        ok, why = _check(alias, "run")
        if not ok:
            return why
        code, out = remote.run(alias, command)
        return f"[{alias} salida {code}]\n{out[:40_000]}"

    box.register(
        ToolSpec("remote.run",
                 "Ejecuta un comando en un servidor. IRREVERSIBLE: un comando "
                 "remoto puede hacer cualquier cosa.",
                 {"type": "object",
                  "properties": {"alias": {"type": "string"},
                                 "command": {"type": "string"}},
                  "required": ["alias", "command"]},
                 mutating=True, reversible=False, danger=2),
        _run,
    )

    # -- escritura remota: reversible vía copia previa --------------------

    def _write(alias: str, path: str, content: str) -> str:
        ok, why = _check(alias, "write")
        if not ok:
            return why
        return remote.write(alias, path, content)

    def _undo_write(act) -> str:
        """
        Un undoer que no puede deshacer tiene que **lanzar**, no devolver el
        fallo como texto.

        `Journal._undo` toma cualquier retorno por éxito y marca la accion como
        UNDONE; solo una excepcion la deja intacta. Devolviendo "fallo al
        restaurar" se conseguia lo peor imaginable en este producto: `fib undo`
        respondia ok, el journal daba la accion por revertida, y el archivo del
        servidor seguia cambiado. Un undo que miente es peor que no tener undo.

        Ademas, la subida usaba su propia copia del `scp` con el puerto mal
        pasado, asi que fallaba siempre.
        """
        alias, path = act.arguments["alias"], act.arguments["path"]
        snap = act.snapshot
        if snap and Path(snap).exists():
            return (f"{remote.push(alias, snap, path)} "
                    f"(copia previa de {alias}:{path})")

        # No existia antes: deshacer = borrarlo.
        code, out = remote.run(alias, f"rm -f {_q(path)}")
        if code != 0:
            raise RemoteError(
                f"no se pudo eliminar {alias}:{path}: {(out or '')[:200]}")
        return f"eliminado {alias}:{path} (no existia antes)"

    box.register(
        ToolSpec("remote.write",
                 "Escribe un archivo en un servidor. REVERSIBLE: se descarga "
                 "una copia previa antes de sobrescribir.",
                 {"type": "object",
                  "properties": {"alias": {"type": "string"},
                                 "path": {"type": "string"},
                                 "content": {"type": "string"}},
                  "required": ["alias", "path", "content"]},
                 mutating=True, danger=2),
        _write,
        undo=_undo_write,
    )

    # Copia previa automatica antes de cada escritura remota.
    _orig = box._maybe_snapshot

    def _snap(name: str, args: dict):
        if name == "remote.write":
            alias, path = args.get("alias"), args.get("path")
            if alias and path and remote.exists(alias, path):
                dest = box.journal.snap_dir / f"{int(__import__('time').time()*1000)}_{alias}_{Path(path).name}"
                if remote.fetch(alias, path, dest):
                    return str(dest)
            return None
        return _orig(name, args)

    box._maybe_snapshot = _snap


def _fmt_probe(p: dict) -> str:
    estado = "accesible" if p["ok"] else "NO responde"
    return (f"{p['alias']} ({p['host']}) — {estado}, ambito={p['scope']}\n\n"
            f"{p['info']}")
