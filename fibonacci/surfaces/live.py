"""
FIBONACCI — Superficies vivas.

Hermes mete cinco SDKs de mensajería dentro del proceso del gateway. Da mucho
de entrada y también acopla el núcleo a cinco APIs que rompen a su ritmo.

Aquí cada superficie es un adaptador de ~80 líneas sobre HTTP puro, sin SDK.
Telegram y Discord se hablan por su API REST con `urllib`. Si mañana Discord
cambia algo, se arregla un archivo y el núcleo ni se entera.

## Lo que ninguna superficie puede saltarse

Toda entrada pasa por `Authority`. Un remitente sin emparejar recibe una
respuesta cortés y nada más — ni ejecución, ni acceso a memoria, ni siquiera
confirmación de que el bot hace algo útil. Exponer un agente con shell a un
chat público sin esto no es una funcionalidad: es una brecha.

## Continuidad entre superficies

`session_key()` decide qué conversaciones comparten historial. Por defecto cada
superficie tiene su sesión, pero si emparejas tu Telegram como el mismo
principal que tu CLI, puedes unificarlas: empiezas en la terminal y sigues en
el teléfono con el mismo contexto.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

from ..identity import Authority, Trust

log = logging.getLogger("fibonacci.surfaces")


@dataclass
class Inbound:
    text: str
    user_id: str
    channel_id: str = ""
    display: str = ""
    attachments: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class Outbound:
    text: str
    files: list[str] = field(default_factory=list)


def _get(url: str, timeout: float = 40.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Fibonacci/0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url: str, payload: dict, headers: dict | None = None,
          timeout: float = 30.0) -> dict:
    h = {"Content-Type": "application/json", "User-Agent": "Fibonacci/0.5"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(errors="replace")[:300], "code": e.code}


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Surface:
    name = "base"

    def session_key(self, msg: Inbound) -> str:
        return f"{self.name}:{msg.channel_id or msg.user_id}"

    def principal_id(self, msg: Inbound) -> str:
        return f"{self.name}:{msg.user_id}"

    def receive(self) -> Iterator[Inbound]:
        raise NotImplementedError

    def send(self, channel_id: str, out: Outbound) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TelegramSurface(Surface):
    """
    Long polling sobre la API Bot. Sin SDK: son dos endpoints.

        export TELEGRAM_BOT_TOKEN=...
        fib serve telegram
    """

    name = "telegram"

    def __init__(self, token: str, poll_timeout: int = 30):
        if not token:
            raise ValueError("falta TELEGRAM_BOT_TOKEN")
        self.base = f"https://api.telegram.org/bot{token}"
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop = threading.Event()

    def receive(self) -> Iterator[Inbound]:
        while not self._stop.is_set():
            try:
                url = (f"{self.base}/getUpdates?timeout={self.poll_timeout}"
                       f"&offset={self._offset}")
                data = _get(url, timeout=self.poll_timeout + 10)
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram poll: %s", exc)
                time.sleep(5)
                continue

            for upd in data.get("result", []):
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                frm = msg.get("from", {})
                text = msg.get("text") or msg.get("caption") or ""
                if not text:
                    continue
                yield Inbound(
                    text=text,
                    user_id=str(frm.get("id", "")),
                    channel_id=str(msg.get("chat", {}).get("id", "")),
                    display=(frm.get("username")
                             or f"{frm.get('first_name','')}".strip()),
                    meta={"message_id": msg.get("message_id")},
                )

    def send(self, channel_id: str, out: Outbound) -> None:
        # Telegram corta en 4096; se parte por líneas para no romper bloques.
        for chunk in _split(out.text, 4000):
            r = _post(f"{self.base}/sendMessage",
                      {"chat_id": channel_id, "text": chunk,
                       "disable_web_page_preview": True})
            if r.get("error"):
                log.error("telegram send: %s", r["error"])

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

class DiscordSurface(Surface):
    """
    Polling REST de canales concretos, no gateway websocket. Para un agente
    personal es suficiente y evita mantener una conexión persistente y su
    protocolo de reconexión.

        export DISCORD_BOT_TOKEN=...
        fib serve discord --channels 123456789
    """

    name = "discord"

    def __init__(self, token: str, channels: list[str], interval: float = 3.0):
        if not token:
            raise ValueError("falta DISCORD_BOT_TOKEN")
        self.token = token
        self.channels = channels
        self.interval = interval
        # Igual que en Telegram: la base es un atributo, no una constante
        # incrustada. Sin esto no hay forma de ejercitar el adaptador sin
        # hablar con Discord de verdad, que es justo lo que no debe hacerse
        # desde una prueba.
        self.base = "https://discord.com/api/v10"
        self._last: dict[str, str] = {}
        self._stop = threading.Event()

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    def receive(self) -> Iterator[Inbound]:
        while not self._stop.is_set():
            for ch in self.channels:
                try:
                    url = f"{self.base}/channels/{ch}/messages?limit=10"
                    if self._last.get(ch):
                        url += f"&after={self._last[ch]}"
                    req = urllib.request.Request(url, headers=self._headers())
                    with urllib.request.urlopen(req, timeout=20) as r:
                        msgs = json.loads(r.read().decode())
                except Exception as exc:  # noqa: BLE001
                    log.warning("discord poll %s: %s", ch, exc)
                    continue

                for m in reversed(msgs):
                    self._last[ch] = m["id"]
                    author = m.get("author", {})
                    if author.get("bot"):
                        continue
                    if not m.get("content"):
                        continue
                    yield Inbound(
                        text=m["content"], user_id=str(author.get("id", "")),
                        channel_id=str(ch),
                        display=author.get("username", ""),
                        meta={"message_id": m["id"]})
            self._stop.wait(self.interval)

    def send(self, channel_id: str, out: Outbound) -> None:
        for chunk in _split(out.text, 1900):
            _post(f"{self.base}/channels/{channel_id}/messages",
                  {"content": chunk}, headers=self._headers())

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Webhook genérico
# ---------------------------------------------------------------------------

class WebhookSurface(Surface):
    """
    Servidor HTTP mínimo para integrar cualquier cosa que sepa hacer POST:
    Slack, Matrix, n8n, un script propio, un formulario web.

    Sin framework: `http.server` de stdlib.
    """

    name = "webhook"

    def __init__(self, host: str = "127.0.0.1", port: int = 8777,
                 secret: str = ""):
        self.host, self.port, self.secret = host, port, secret
        self._queue: list[Inbound] = []
        self._lock = threading.Lock()
        self._replies: dict[str, str] = {}
        self._server = None

    def start(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        surface = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                if surface.secret and self.headers.get(
                        "X-Fibonacci-Secret") != surface.secret:
                    self.send_response(403)
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(n).decode())
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                msg = Inbound(
                    text=body.get("text", ""),
                    user_id=str(body.get("user_id", "webhook")),
                    channel_id=str(body.get("channel_id", "default")),
                    display=body.get("display", ""))
                with surface._lock:
                    surface._queue.append(msg)

                # Espera acotada por la respuesta del agente.
                key = f"{msg.channel_id}"
                for _ in range(600):
                    if key in surface._replies:
                        reply = surface._replies.pop(key)
                        payload = json.dumps({"text": reply}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    time.sleep(0.1)
                self.send_response(202)
                self.end_headers()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        log.info("webhook escuchando en http://%s:%d", self.host, self.port)

    def receive(self) -> Iterator[Inbound]:
        if self._server is None:
            self.start()
        while True:
            with self._lock:
                pend = self._queue[:]
                self._queue.clear()
            for m in pend:
                yield m
            time.sleep(0.2)

    def send(self, channel_id: str, out: Outbound) -> None:
        self._replies[channel_id] = out.text

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


# ---------------------------------------------------------------------------
# El bucle que las conecta al agente
# ---------------------------------------------------------------------------

UNPAIRED = (
    "Hola. Este agente es personal y aún no estás autorizado.\n\n"
    "Si eres su dueño, ejecuta `fib pair` en tu terminal y envíame el código "
    "que aparezca.")

PAIRED_OK = "Listo, quedaste emparejado. Ya puedes pedirme cosas."


class SurfaceRunner:
    """
    Conecta una superficie al agente aplicando identidad, emparejamiento y
    aislamiento de sesión.
    """

    def __init__(self, agent, surface: Surface, authority: Authority | None = None,
                 shared_session: str | None = None):
        self.agent = agent
        self.surface = surface
        self.authority = authority or Authority.load()
        self.shared_session = shared_session

    def run(self) -> None:
        log.info("Superficie '%s' activa", self.surface.name)
        for msg in self.surface.receive():
            try:
                self._handle(msg)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error atendiendo mensaje")
                self.surface.send(msg.channel_id, Outbound(
                    f"Algo falló de mi lado: {exc}"))

    def _handle(self, msg: Inbound) -> None:
        pid = self.surface.principal_id(msg)
        principal = self.authority.principal(pid)

        # Emparejamiento: el único camino de UNKNOWN a MEMBER.
        if principal.trust == Trust.UNKNOWN:
            code = msg.text.strip()
            if self.authority.pair(pid, code, msg.display or pid, Trust.MEMBER):
                self.surface.send(msg.channel_id, Outbound(PAIRED_OK))
                log.info("Emparejado %s desde %s", pid, self.surface.name)
            else:
                self.surface.send(msg.channel_id, Outbound(UNPAIRED))
            return

        session = self.shared_session or self.surface.session_key(msg)

        # En una superficie remota nadie puede confirmar en vivo, así que todo
        # lo que exigiría confirmación se rechaza con explicación. Un "sí"
        # implícito por chat sería exactamente la puerta que no queremos.
        prev_confirm = self.agent.tools.confirm
        rechazos: list[str] = []

        def confirm_remoto(desc: str, danger: int) -> bool:
            rechazos.append(desc[:140])
            return False

        self.agent.tools.confirm = confirm_remoto
        try:
            reply = self.agent.chat(msg.text, session, surface=self.surface.name)
        finally:
            self.agent.tools.confirm = prev_confirm

        texto = reply.text
        if rechazos:
            texto += ("\n\n⚠ No ejecuté esto porque requiere tu confirmación y "
                      "no puedo pedírtela por aquí:\n"
                      + "\n".join(f"· {r}" for r in rechazos[:3])
                      + "\n\nHazlo desde la terminal con `fib`.")
        if reply.actions:
            texto += (f"\n\n↶ {len(reply.actions)} cambio(s) reversibles: "
                      f"`fib undo --all -s {session}`")

        self.surface.send(msg.channel_id, Outbound(texto))


def _split(text: str, limit: int) -> list[str]:
    """Parte respeta líneas: cortar a la mitad un bloque de código es peor que
    mandar un mensaje más."""
    if len(text) <= limit:
        return [text]
    out, actual = [], ""
    for linea in text.split("\n"):
        if len(actual) + len(linea) + 1 > limit:
            if actual:
                out.append(actual)
            actual = linea[:limit]
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        out.append(actual)
    return out


def build(name: str, **kw) -> Surface:
    import os

    if name == "telegram":
        return TelegramSurface(kw.get("token") or os.environ.get(
            "TELEGRAM_BOT_TOKEN", ""))
    if name == "discord":
        return DiscordSurface(
            kw.get("token") or os.environ.get("DISCORD_BOT_TOKEN", ""),
            kw.get("channels") or [])
    if name == "webhook":
        return WebhookSurface(port=kw.get("port", 8777),
                              secret=kw.get("secret", ""))
    raise ValueError(f"superficie desconocida: {name}")
