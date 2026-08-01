"""
FIBONACCI — Servidor MCP.

Expone el agente a Claude Code, Cursor, Hermes o cualquier host MCP. Lo que lo
hace distinto de otros servidores MCP: expone también `fibonacci_undo`. El
host puede revertir lo que hizo, algo que ninguna otra herramienta MCP ofrece.

    claude mcp add fibonacci -- python3 -m fibonacci.mcp
"""

from __future__ import annotations

import json
import sys

from . import __version__, boot

TOOLS = [
    {"name": "fibonacci_do",
     "description": "Ejecuta una tarea con herramientas reales (archivos, shell, "
                    "web). Los cambios en archivos son reversibles con fibonacci_undo.",
     "inputSchema": {"type": "object",
                     "properties": {"instruction": {"type": "string"},
                                    "session": {"type": "string", "default": "mcp"}},
                     "required": ["instruction"]}},
    {"name": "fibonacci_undo",
     "description": "Revierte la última acción del agente, o toda la sesión con "
                    "all=true. Úsalo si el resultado no fue el esperado.",
     "inputSchema": {"type": "object",
                     "properties": {"session": {"type": "string", "default": "mcp"},
                                    "all": {"type": "boolean", "default": False}}}},
    {"name": "fibonacci_history",
     "description": "Qué ha modificado el agente y qué sigue siendo reversible.",
     "inputSchema": {"type": "object",
                     "properties": {"session": {"type": "string"},
                                    "limit": {"type": "integer", "default": 20}}}},
    {"name": "fibonacci_recall",
     "description": "Consulta lo que el agente sabe del usuario, con la confianza "
                    "ya ajustada por antigüedad.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
]


class MCPServer:
    def __init__(self):
        self._agent = None

    @property
    def agent(self):
        if self._agent is None:
            self._agent = boot()
        return self._agent

    def handle(self, req: dict) -> dict | None:
        method, rid = req.get("method", ""), req.get("id")
        if method == "initialize":
            return _ok(rid, {"protocolVersion": "2024-11-05",
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "fibonacci", "version": __version__}})
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return _ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            p = req.get("params", {})
            try:
                return _ok(rid, {"content": [{"type": "text",
                                              "text": self._call(p.get("name", ""),
                                                                 p.get("arguments", {}))}]})
            except Exception as exc:  # noqa: BLE001
                return _ok(rid, {"content": [{"type": "text", "text": f"Error: {exc}"}],
                                 "isError": True})
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": method}}

    def _call(self, name: str, a: dict) -> str:
        session = a.get("session", "mcp")
        if name == "fibonacci_do":
            r = self.agent.chat(a["instruction"], session, surface="mcp")
            head = f"[herramientas: {', '.join(r.tools_used) or 'ninguna'}]"
            if r.actions:
                head += f"\n[{len(r.actions)} cambio(s) reversibles con fibonacci_undo]"
            return head + "\n\n" + r.text
        if name == "fibonacci_undo":
            if a.get("all"):
                n, notes = self.agent.journal.undo_session(session)
                return f"{n} revertidas:\n" + "\n".join(notes)
            ok, msg = self.agent.journal.undo_last(session)
            return msg
        if name == "fibonacci_history":
            acts = self.agent.journal.history(a.get("session"), a.get("limit", 20))
            if not acts:
                return "Sin cambios registrados."
            return "\n".join(
                f"{x.status.value:12s} {x.tool:14s} "
                f"{json.dumps(x.arguments, ensure_ascii=False)[:70]}" for x in acts)
        if name == "fibonacci_recall":
            notes = self.agent.memory.recall(a["query"], k=10)
            if not notes:
                return "Sin datos relevantes."
            return "\n".join(
                f"- [{n.kind}] {n.content} (confianza {n.current_confidence():.0%})"
                for n in notes)
        raise ValueError(f"Herramienta desconocida: {name}")


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> int:
    s = MCPServer()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = s.handle(json.loads(line))
        except json.JSONDecodeError:
            continue
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
