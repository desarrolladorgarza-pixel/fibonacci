"""
Fixtures compartidas de Fibonacci.

Dos cosas que este archivo resuelve y que antes hacían imposible probar bien:

**1. Aislamiento.** Hasta la v0.6.0 las pruebas escribían en el directorio de
datos REAL del usuario (`~/.local/share/fibonacci`). Eso contaminaba la bóveda,
el journal y la forja de quien corriera `pytest`, y hacía que dos ejecuciones
seguidas no fueran independientes. `isolate` parchea `data_dir` y `config_dir`
globalmente, con `autouse=True`: ninguna prueba puede escaparse.

**2. Un modelo falso que habla el protocolo real.** El mesh, el router, los
proveedores y el bucle de herramientas del agente nunca se probaron porque
requerían un LLM. `fake_model` levanta un servidor HTTP que responde el formato
OpenAI de verdad —incluidos tool_calls y streaming SSE—, así que todo ese
camino se puede ejercitar sin red y sin GPU.

El servidor es programable: le dices qué debe responder ante cada petición, o
le pasas un guion de respuestas en orden. Con eso se pueden probar cosas que
antes eran inalcanzables: que la cascada degrade al segundo modelo, que el
circuit breaker abra, que el bucle de herramientas se corte en el tope, que el
presupuesto interrumpa a media conversación.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


# ---------------------------------------------------------------------------
# Aislamiento
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """
    Cada prueba corre en su propio sistema de archivos. `autouse` es
    deliberado: si esto fuera opcional, alguien olvidaría pedirlo y volvería a
    ensuciar el home del usuario.
    """
    datos = tmp_path / "data"
    conf = tmp_path / "config"
    trabajo = tmp_path / "workspace"
    for d in (datos, conf, trabajo):
        d.mkdir(parents=True, exist_ok=True)

    import fibonacci.platform as plat

    monkeypatch.setattr(plat, "data_dir", lambda: datos)
    monkeypatch.setattr(plat, "config_dir", lambda: conf)
    monkeypatch.setattr(plat, "workspace", lambda: trabajo)

    # Los módulos que importaron el símbolo directamente necesitan su propio
    # parche: `from .platform import data_dir` copia la referencia.
    #
    # `cli` va en la lista aunque parezca que no hace falta: sí hace falta.
    # Sin él, el aislamiento del CLI dependía de que `fibonacci.cli` se
    # importara por primera vez dentro de una prueba —lo cual era cierto por
    # accidente—. Cualquier import a nivel de módulo lo habría apuntado al
    # home real del usuario.
    for mod in ("journal", "memory", "tasks", "scheduler", "forge", "sync",
                "control", "api", "identity", "tools", "cli", "mcp",
                "subagents"):
        try:
            m = __import__(f"fibonacci.{mod}", fromlist=["x"])
        except ImportError:
            continue
        for nombre, valor in (("data_dir", lambda: datos),
                              ("config_dir", lambda: conf),
                              ("workspace", lambda: trabajo)):
            if hasattr(m, nombre):
                monkeypatch.setattr(m, nombre, valor)

    yield {"data": datos, "config": conf, "workspace": trabajo}


@pytest.fixture
def workspace(isolate):
    return isolate["workspace"]


# ---------------------------------------------------------------------------
# Piezas del núcleo
# ---------------------------------------------------------------------------

@pytest.fixture
def journal(tmp_path):
    from fibonacci.journal import Journal

    return Journal(tmp_path / "journal.db", snapshots=tmp_path / "snapshots")


@pytest.fixture
def memory(tmp_path):
    from fibonacci.memory import Memory

    return Memory(tmp_path / "memory.db")


@pytest.fixture
def toolbox(journal, workspace):
    """ToolBox con confirmación automática. Para probar el camino de negación,
    construye uno con `confirm=lambda d, x: False`."""
    from fibonacci.tools import ToolBox

    return ToolBox(journal, root=workspace, confirm=lambda desc, danger: True)


# ---------------------------------------------------------------------------
# Servidor de modelo falso
# ---------------------------------------------------------------------------

class FakeModelServer:
    """
    Endpoint compatible con OpenAI, programable.

        fake.reply("hola")                      # siguiente respuesta
        fake.reply_tool("file.read", {"path": "x"})   # pide una herramienta
        fake.fail(500)                          # el siguiente falla
        fake.script(["uno", "dos"])             # guion en orden

    Registra todo lo recibido en `fake.requests`, para poder afirmar sobre lo
    que el agente envió: que el presupuesto de contexto se respetó, que las
    skills entraron al system, que los tool results volvieron.
    """

    def __init__(self, port: int = 0):
        self.port = port
        self.requests: list[dict] = []
        self._queue: list[dict] = []
        self._default = {"kind": "text", "text": "respuesta por defecto"}
        self._srv: ThreadingHTTPServer | None = None
        self.models = ["qwen3:8b", "gpt-oss:120b", "deepseek-r1:70b", "bge-m3"]

    # -- programación ---------------------------------------------------

    def reply(self, text: str) -> "FakeModelServer":
        self._queue.append({"kind": "text", "text": text})
        return self

    def reply_tool(self, name: str, args: dict | None = None,
                   text: str = "") -> "FakeModelServer":
        self._queue.append({"kind": "tool", "name": name,
                            "args": args or {}, "text": text})
        return self

    def reply_json(self, obj) -> "FakeModelServer":
        self._queue.append({"kind": "text", "text": json.dumps(obj)})
        return self

    def fail(self, status: int = 500, times: int = 1) -> "FakeModelServer":
        for _ in range(times):
            self._queue.append({"kind": "fail", "status": status})
        return self

    def hang(self, seconds: float) -> "FakeModelServer":
        self._queue.append({"kind": "hang", "seconds": seconds})
        return self

    def script(self, items: list) -> "FakeModelServer":
        for it in items:
            self.reply(it) if isinstance(it, str) else self._queue.append(it)
        return self

    def default(self, text: str) -> "FakeModelServer":
        self._default = {"kind": "text", "text": text}
        return self

    def reset(self) -> None:
        self._queue.clear()
        self.requests.clear()

    def _next(self) -> dict:
        return self._queue.pop(0) if self._queue else dict(self._default)

    # -- servidor -------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeModelServer":
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):  # noqa: N802
                if self.path.endswith("/models"):
                    self._json({"data": [{"id": m} for m in server.models]})
                else:
                    self._json({"ok": True})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                server.requests.append({"path": self.path, "body": body,
                                        "headers": dict(self.headers)})

                if self.path.endswith("/embeddings"):
                    entradas = body.get("input") or []
                    if isinstance(entradas, str):
                        entradas = [entradas]
                    self._json({"data": [
                        {"embedding": _pseudo_vector(t)} for t in entradas]})
                    return

                accion = server._next()
                if accion["kind"] == "fail":
                    self._json({"error": {"message": "fallo simulado"}},
                               accion.get("status", 500))
                    return
                if accion["kind"] == "hang":
                    time.sleep(accion["seconds"])
                    accion = {"kind": "text", "text": "tarde"}

                if body.get("stream"):
                    self._stream(accion.get("text", ""))
                    return

                self._json(_completion(accion, body.get("model", "fake")))

            def _stream(self, text: str):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for palabra in text.split(" "):
                    trozo = json.dumps({"choices": [
                        {"delta": {"content": palabra + " "}}]})
                    dato = f"data: {trozo}\n\n".encode()
                    self.wfile.write(f"{len(dato):X}\r\n".encode() + dato + b"\r\n")
                    self.wfile.flush()
                fin = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(fin):X}\r\n".encode() + fin + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    # -- aserciones de conveniencia -------------------------------------

    def last_body(self) -> dict:
        return self.requests[-1]["body"] if self.requests else {}

    def last_system(self) -> str:
        for m in self.last_body().get("messages", []):
            if m.get("role") == "system":
                return m.get("content", "")
        return ""

    def last_tools(self) -> list[str]:
        return [t["function"]["name"]
                for t in self.last_body().get("tools", [])
                if "function" in t]

    def approx_prompt_chars(self) -> int:
        return sum(len(m.get("content") or "")
                   for m in self.last_body().get("messages", []))


def _completion(accion: dict, model: str) -> dict:
    msg: dict = {"role": "assistant", "content": accion.get("text", "")}
    if accion["kind"] == "tool":
        msg["tool_calls"] = [{
            "id": f"call_{int(time.time()*1000)}",
            "type": "function",
            "function": {"name": accion["name"],
                         "arguments": json.dumps(accion["args"])}}]
    return {"id": "cmpl-fake", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "total_tokens": 120}}


def _pseudo_vector(text: str, dims: int = 32) -> list[float]:
    """
    Vector determinista por texto: textos iguales dan el mismo vector y textos
    parecidos quedan cerca. Suficiente para probar recuperación.

    Se construye sumando un hash **por palabra**, no hasheando el texto
    completo. La versión anterior hacía lo segundo y por eso incumplía su
    propia promesa: dos frases que compartían casi todo el vocabulario salían
    tan ortogonales como dos frases sin relación —a veces con coseno negativo—
    y `Memory.recall` las descartaba. El efecto era que ninguna prueba podía
    ejercitar de verdad el camino semántico de la memoria.
    """
    import hashlib
    import math
    import re

    palabras = re.findall(r"\w+", text.lower()) or [text.lower()]
    vec = [0.0] * dims
    for palabra in palabras:
        h = hashlib.sha256(palabra.encode()).digest()
        for i in range(dims):
            vec[i] += (h[i % len(h)] / 255.0) - 0.5
    norma = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norma for x in vec]


@pytest.fixture
def fake_model():
    s = FakeModelServer().start()
    yield s
    s.stop()


@pytest.fixture
def mesh(fake_model):
    """Mesh apuntando al modelo falso. Todo local, sin salir a red."""
    from fibonacci.mesh.providers import OpenAICompatProvider
    from fibonacci.mesh.registry import Catalog
    from fibonacci.mesh.router import ModelMesh

    prov = OpenAICompatProvider("ollama", f"{fake_model.base_url}/v1")
    return ModelMesh(Catalog.from_profile("local"), {"ollama": prov}, mode="local")


@pytest.fixture
def agent(mesh, memory, journal, toolbox):
    """Agente completo sobre el modelo falso. Permite probar el bucle entero."""
    from fibonacci.agent import Agent, SpendBudget

    memory.embedder = mesh.embed
    return Agent(mesh, memory, journal, toolbox,
                 budget=SpendBudget(max_usd=1.0, max_seconds=30))


# ---------------------------------------------------------------------------
# Servidor HTTP genérico (para api.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def http_server():
    """Servidor de eco programable, para probar el cliente de APIs."""
    rutas: dict[str, tuple[int, dict]] = {}
    recibidas: list[dict] = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _responder(self):
            n = int(self.headers.get("Content-Length", 0))
            cuerpo = self.rfile.read(n) if n else b""
            recibidas.append({"method": self.command, "path": self.path,
                              "headers": dict(self.headers),
                              "body": cuerpo.decode(errors="replace")})
            clave = self.path.split("?")[0]
            code, payload = rutas.get(clave, (200, {"ok": True, "ruta": self.path}))
            b = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _responder

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)

    class Handle:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        requests = recibidas

        @staticmethod
        def route(path: str, payload, status: int = 200):
            rutas[path] = (status, payload)

    yield Handle()
    srv.shutdown()
    srv.server_close()
