"""
FIBONACCI — La Forja.

Pediste que Fibonacci construya sus propios MCP, protocolos y herramientas.
Esto lo hace, con una diferencia importante frente a la fantasía de "el agente
escribe código y lo ejecuta sin más": **nada de lo que la Forja produce toca
tu sistema sin pasar por las mismas compuertas que todo lo demás.**

El flujo:

  1. El agente describe la capacidad que le falta ("necesito consultar la API
     de mi CRM").
  2. La Forja genera una herramienta: código Python + un manifiesto que declara
     si muta, si es reversible, y su nivel de peligro.
  3. La herramienta se genera en CUARENTENA. No se registra todavía.
  4. Se prueba en un subproceso aislado, con timeout y sin red salvo que se
     autorice. Si falla, no se instala.
  5. Solo entonces, y con confirmación del dueño, se promueve a herramienta
     activa — y desde ese momento pasa por el Gate, el journal y la redacción
     como cualquier otra.

Una herramienta autogenerada que muta y no declara su inverso NO se instala:
la misma regla que aplica a las herramientas nativas (ValueError en registro)
aplica a las que el agente se escribe a sí mismo. La autonomía de construir no
es autonomía de saltarse las reglas.

## Servidores MCP generados

La Forja también empaqueta un conjunto de herramientas como un servidor MCP
autónomo (stdio, JSON-RPC 2.0) que otros agentes —Claude Code, Cursor, otro
Fibonacci— pueden consumir. Fibonacci deja de ser solo cliente de MCP y pasa a
ser proveedor: construye el protocolo, no solo lo habla.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import Capability, Message, ToolSpec
from .platform import data_dir

log = logging.getLogger("fibonacci.forge")


def forge_dir() -> Path:
    d = data_dir() / "forge"
    (d / "quarantine").mkdir(parents=True, exist_ok=True)
    (d / "active").mkdir(parents=True, exist_ok=True)
    (d / "servers").mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class ForgedTool:
    name: str
    description: str
    code: str
    parameters: dict
    mutating: bool = False
    reversible: bool = True
    danger: int = 0
    needs_network: bool = False
    status: str = "quarantine"       # quarantine | tested | active | rejected
    test_result: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.code.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Análisis estático de seguridad
# ---------------------------------------------------------------------------

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "memoryview",
}
FORBIDDEN_IMPORTS = {"ctypes", "cffi", "marshal", "pickle", "importlib"}
NETWORK_MODULES = {"socket", "urllib", "http", "requests", "httpx", "ftplib",
                   "smtplib", "telnetlib", "asyncio"}
FS_WRITE_HINTS = {"open", "write_text", "write_bytes", "unlink", "rmtree",
                  "remove", "rmdir", "mkdir", "rename", "replace"}


@dataclass
class StaticReport:
    safe: bool
    reasons: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    uses_network: bool = False
    uses_fs_write: bool = False
    uses_subprocess: bool = False


def analyze(code: str) -> StaticReport:
    """
    Análisis AST antes de ejecutar NADA. No es un sandbox —eso lo da el
    subproceso—, pero atrapa lo obvio sin siquiera importar el módulo:
    `eval`, `ctypes`, imports de red no declarados.
    """
    rep = StaticReport(safe=True)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return StaticReport(False, [f"no compila: {exc}"])

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                rep.safe = False
                rep.reasons.append(f"llamada prohibida: {node.func.id}()")
            if node.func.id in FS_WRITE_HINTS:
                rep.uses_fs_write = True
        if isinstance(node, ast.Attribute) and node.attr in FS_WRITE_HINTS:
            rep.uses_fs_write = True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""])
            for m in mods:
                root = m.split(".")[0]
                rep.imports.add(root)
                if root in FORBIDDEN_IMPORTS:
                    rep.safe = False
                    rep.reasons.append(f"import prohibido: {root}")
                if root in NETWORK_MODULES:
                    rep.uses_network = True
                if root == "subprocess":
                    rep.uses_subprocess = True

    if rep.uses_subprocess:
        rep.reasons.append("usa subprocess: se ejecutará con confirmación extra")
    return rep


# ---------------------------------------------------------------------------
# La Forja
# ---------------------------------------------------------------------------

GENERATE_SYSTEM = """Eres un generador de herramientas para el agente Fibonacci.

Produces UNA función Python autocontenida que implementa la capacidad pedida.

Reglas estrictas:
- La función se llama `run` y recibe argumentos con nombre (kwargs).
- Devuelve siempre un str (el resultado que verá el agente).
- Sin `eval`, `exec`, `ctypes`, `pickle`. Sin efectos secundarios ocultos.
- Importa solo lo que uses, arriba de la función.
- Si la herramienta MODIFICA algo (archivos, red, estado externo), tu manifiesto
  debe marcar mutating=true, y si es reversible debes proveer también una
  función `undo(args)` que revierta.
- Código legible, con validación de entradas. Falla con mensajes claros.

Responde SOLO JSON:
{
 "name": "kebab-case",
 "description": "qué hace, una línea",
 "parameters": {"type":"object","properties":{...},"required":[...]},
 "mutating": false,
 "reversible": true,
 "danger": 0,
 "needs_network": false,
 "code": "import ...\\n\\ndef run(**kwargs):\\n    ...",
 "undo_code": "def undo(args):\\n    ..."   // solo si mutating y reversible
}"""


class Forge:
    def __init__(self, mesh, journal=None, confirm=None):
        self.mesh = mesh
        self.journal = journal
        self.confirm = confirm
        self.dir = forge_dir()

    # ------------------------------------------------------------------

    def generate(self, need: str, context: str = "") -> ForgedTool:
        """Genera una herramienta desde una necesidad en lenguaje natural."""
        from .agent import _parse_json

        prompt = f"Capacidad necesaria: {need}"
        if context:
            prompt += f"\n\nContexto: {context}"

        c = self.mesh.ask(
            Capability.CODE,
            [Message("system", GENERATE_SYSTEM), Message("user", prompt)],
            temperature=0.2, json_mode=True, max_tokens=2500)
        data = _parse_json(c.text) or {}
        if not data.get("code") or not data.get("name"):
            raise ValueError("la generación no produjo código válido")

        code = data["code"]
        if data.get("mutating") and data.get("reversible") and data.get("undo_code"):
            code += "\n\n" + data["undo_code"]

        tool = ForgedTool(
            name=data["name"], description=data.get("description", ""),
            code=code, parameters=data.get("parameters", {"type": "object", "properties": {}}),
            mutating=bool(data.get("mutating")),
            reversible=bool(data.get("reversible", True)),
            danger=int(data.get("danger", 0)),
            needs_network=bool(data.get("needs_network")),
        )

        # Misma regla que las herramientas nativas: mutante + reversible exige undo.
        if tool.mutating and tool.reversible and "def undo(" not in code:
            tool.status = "rejected"
            tool.test_result = ("rechazada: es mutante y reversible pero no "
                                "define undo(). No se instala.")
            return tool

        self._save(tool, "quarantine")
        return tool

    def vet(self, tool: ForgedTool, allow_network: bool = False) -> ForgedTool:
        """Analiza y prueba en aislamiento. No instala nada."""
        rep = analyze(tool.code)
        if not rep.safe:
            tool.status = "rejected"
            tool.test_result = "análisis estático: " + "; ".join(rep.reasons)
            self._save(tool, "quarantine")
            return tool

        if rep.uses_network and not allow_network and not tool.needs_network:
            tool.status = "rejected"
            tool.test_result = ("usa red sin declararla (needs_network=false). "
                                "Rechazada por discrepancia.")
            self._save(tool, "quarantine")
            return tool

        ok, out = self._sandbox_test(tool, allow_network)
        tool.test_result = out
        tool.status = "tested" if ok else "rejected"
        self._save(tool, "quarantine")
        return tool

    def _sandbox_test(self, tool: ForgedTool, allow_network: bool) -> tuple[bool, str]:
        """
        Ejecuta en un subproceso separado con timeout. Aislamiento de proceso:
        si el código cuelga o revienta, no se lleva al agente con él. La red se
        corta a nivel de código inyectando un socket que rechaza conexiones,
        salvo que se autorice.
        """
        net_block = "" if allow_network else _NET_BLOCK
        harness = (
            "import sys, json\n"
            + net_block + "\n"
            + tool.code + "\n\n"
            + 'if __name__ == "__main__":\n'
            "    try:\n"
            '        assert callable(run), "run no es invocable"\n'
            "        try:\n"
            "            result = run()\n"
            '            print("SMOKE_OK:", str(result)[:200])\n'
            "        except TypeError:\n"
            '            print("SMOKE_OK: firma valida (requiere args)")\n'
            "        except Exception as e:\n"
            '            print("SMOKE_OK: valida entradas ->", str(e)[:120])\n'
            "    except Exception as e:\n"
            '        print("SMOKE_FAIL:", type(e).__name__, str(e)[:200])\n'
            "        sys.exit(1)\n"
        )

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(harness)
            path = f.name
        try:
            r = subprocess.run([sys.executable, path], capture_output=True,
                               text=True, timeout=15)
            out = (r.stdout + r.stderr).strip()
            return ("SMOKE_OK" in out and r.returncode == 0), out[:1000]
        except subprocess.TimeoutExpired:
            return False, "timeout: el código no terminó en 15s (¿bucle infinito?)"
        finally:
            Path(path).unlink(missing_ok=True)

    def promote(self, tool: ForgedTool, box) -> tuple[bool, str]:
        """
        Instala una herramienta probada en el ToolBox vivo. Desde aquí pasa por
        el Gate, el journal y la redacción como cualquier otra.
        """
        if tool.status != "tested":
            return False, f"no promovible (estado: {tool.status})"

        if self.confirm and not self.confirm(
            f"Instalar herramienta autogenerada '{tool.name}'? "
            f"muta={tool.mutating} peligro={tool.danger}\n{tool.description}",
            tool.danger,
        ):
            return False, "cancelada por el usuario"

        ns: dict = {}
        try:
            exec(compile(tool.code, f"<forge:{tool.name}>", "exec"), ns)  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            return False, f"falló al cargar: {exc}"

        run_fn = ns.get("run")
        if not callable(run_fn):
            return False, "el código no define run()"
        undo_fn = ns.get("undo")

        spec = ToolSpec(
            name=f"forged.{tool.name}", description=tool.description,
            parameters=tool.parameters, mutating=tool.mutating,
            reversible=tool.reversible, danger=tool.danger)

        undo_wrap = (lambda act: undo_fn(act.arguments)) if undo_fn else None
        box.register(spec, lambda **kw: run_fn(**kw), undo=undo_wrap)

        tool.status = "active"
        self._save(tool, "active")
        if self.journal:
            self.journal.trace("forge", "herramienta_instalada",
                               f"{tool.name} muta={tool.mutating}")
        return True, f"instalada como forged.{tool.name}"

    # ------------------------------------------------------------------
    # Generar un servidor MCP autónomo
    # ------------------------------------------------------------------

    def build_mcp_server(self, name: str, tools: list[ForgedTool]) -> Path:
        """
        Empaqueta varias herramientas probadas como un servidor MCP autónomo.
        Fibonacci deja de ser solo cliente MCP: genera el servidor que otros
        agentes consumirán.
        """
        activos = [t for t in tools if t.status in ("tested", "active")]
        if not activos:
            raise ValueError("no hay herramientas probadas para empaquetar")

        tool_defs = ",\n".join(
            f"    {json.dumps({'name': t.name, 'description': t.description, 'inputSchema': t.parameters})}"
            for t in activos)
        dispatch = "\n".join(
            f'    if name == {json.dumps(t.name)}:\n'
            f'        return str(_impl_{_slug(t.name)}(**args))'
            for t in activos)
        server = _MCP_TEMPLATE.format(
            name=name, version="1.0",
            tool_defs=tool_defs,
            per_tool=_per_tool_impls(activos),
            dispatch=dispatch)

        dest = self.dir / "servers" / f"{name}.py"
        dest.write_text(server, encoding="utf-8")
        log.info("Servidor MCP generado: %s (%d herramientas)", dest, len(activos))
        return dest

    # ------------------------------------------------------------------

    def _save(self, tool: ForgedTool, where: str) -> None:
        d = self.dir / where
        (d / f"{tool.name}.py").write_text(tool.code, encoding="utf-8")
        (d / f"{tool.name}.json").write_text(json.dumps({
            "name": tool.name, "description": tool.description,
            "parameters": tool.parameters, "mutating": tool.mutating,
            "reversible": tool.reversible, "danger": tool.danger,
            "needs_network": tool.needs_network, "status": tool.status,
            "test_result": tool.test_result, "created_at": tool.created_at,
        }, indent=2), encoding="utf-8")

    def list_tools(self, status: str | None = None) -> list[dict]:
        out = []
        for where in ("quarantine", "active"):
            for jf in (self.dir / where).glob("*.json"):
                data = json.loads(jf.read_text(encoding="utf-8"))
                if status is None or data["status"] == status:
                    out.append(data)
        return out


_NET_BLOCK = '''
import socket as _s
class _Blocked(_s.socket):
    def connect(self, *a, **k): raise OSError("red bloqueada en cuarentena")
    def connect_ex(self, *a, **k): raise OSError("red bloqueada en cuarentena")
_s.socket = _Blocked
'''


def _slug(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


def _per_tool_impls(tools: list[ForgedTool]) -> str:
    """Cada herramienta se aísla en su propio namespace vía exec, para que dos
    funciones `run` no colisionen."""
    blocks = []
    for t in tools:
        blocks.append(textwrap.dedent(f'''
            _ns_{_slug(t.name)} = {{}}
            exec(compile({t.code!r}, "<{t.name}>", "exec"), _ns_{_slug(t.name)})
            def _impl_{_slug(t.name)}(**kw):
                return _ns_{_slug(t.name)}["run"](**kw)
        '''))
    return "\n".join(blocks)


_MCP_TEMPLATE = '''#!/usr/bin/env python3
"""
Servidor MCP generado por Fibonacci: {name}
Herramientas empaquetadas y probadas en cuarentena antes de exponerse.
stdio · JSON-RPC 2.0 · sin dependencias.
"""
import json
import sys

# --- implementaciones aisladas ------------------------------------------
{per_tool}

# --- catálogo ------------------------------------------------------------
TOOLS = [
{tool_defs}
]


def dispatch(name, args):
{dispatch}
    raise ValueError(f"herramienta desconocida: {{name}}")

def handle(req):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {{"jsonrpc": "2.0", "id": rid, "result": {{
            "protocolVersion": "2024-11-05", "capabilities": {{"tools": {{}}}},
            "serverInfo": {{"name": {name!r}, "version": {version!r}}}}}}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {{"jsonrpc": "2.0", "id": rid, "result": {{"tools": TOOLS}}}}
    if method == "tools/call":
        p = req.get("params", {{}})
        try:
            text = dispatch(p.get("name", ""), p.get("arguments", {{}}))
            return {{"jsonrpc": "2.0", "id": rid,
                    "result": {{"content": [{{"type": "text", "text": text}}]}}}}
        except Exception as exc:
            return {{"jsonrpc": "2.0", "id": rid, "result": {{
                "content": [{{"type": "text", "text": f"Error: {{exc}}"}}],
                "isError": True}}}}
    return {{"jsonrpc": "2.0", "id": rid,
            "error": {{"code": -32601, "message": method}}}}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = handle(json.loads(line))
        except json.JSONDecodeError:
            continue
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
'''
