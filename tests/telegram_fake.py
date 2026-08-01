"""
Imitación fiel de la Bot API de Telegram, para validar `TelegramSurface`.

Reproduce lo que importa del servidor real:

  · `getUpdates` respeta `offset`: confirmar hasta N descarta 0..N y no los
    vuelve a entregar. Es el detalle que, si está mal, hace que el bot procese
    el mismo mensaje en bucle para siempre.
  · `sendMessage` rechaza con 400 cualquier texto de más de 4096 caracteres,
    igual que el servidor real.
  · Los updates traen la forma real: `message.from.id`, `message.chat.id`,
    `caption` en vez de `text` cuando es una foto, `edited_message`.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LIMITE_TELEGRAM = 4096


class FakeTelegram:
    def __init__(self):
        self.updates = []          # pendientes de entregar
        self.enviados = []         # {chat_id, text}
        self.peticiones = []       # rutas pedidas, para afirmar sobre offset
        self._siguiente_id = 1
        self._srv = None
        self.port = 0

    # -- programación ----------------------------------------------------

    def mensaje(self, texto, user_id="7", chat_id="c1", username="ana",
                editado=False, como_caption=False):
        cuerpo = {
            "message_id": self._siguiente_id,
            "from": {"id": int(user_id), "username": username,
                     "first_name": "Ana"},
            "chat": {"id": chat_id, "type": "private"},
        }
        cuerpo["caption" if como_caption else "text"] = texto
        clave = "edited_message" if editado else "message"
        self.updates.append({"update_id": self._siguiente_id, clave: cuerpo})
        self._siguiente_id += 1
        return self

    def evento_sin_texto(self):
        """Un update que no es un mensaje de texto: no debe llegar al agente."""
        self.updates.append({"update_id": self._siguiente_id,
                             "poll": {"id": "x", "question": "?"}})
        self._siguiente_id += 1
        return self

    # -- servidor --------------------------------------------------------

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
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

            def do_GET(self):
                server.peticiones.append(self.path)
                if "/getUpdates" in self.path:
                    q = parse_qs(urlparse(self.path).query)
                    offset = int(q.get("offset", ["0"])[0])
                    if offset:
                        # El servidor real descarta los confirmados.
                        server.updates = [u for u in server.updates
                                          if u["update_id"] >= offset]
                    pendientes = list(server.updates)
                    self._json({"ok": True, "result": pendientes})
                else:
                    self._json({"ok": True, "result": []})

            def do_POST(self):
                server.peticiones.append(self.path)
                n = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                if "/sendMessage" in self.path:
                    texto = body.get("text", "")
                    if len(texto) > LIMITE_TELEGRAM:
                        self._json({"ok": False, "error_code": 400,
                                    "description": "message is too long"}, 400)
                        return
                    server.enviados.append({"chat_id": body.get("chat_id"),
                                            "text": texto})
                    self._json({"ok": True, "result": {"message_id": 1}})
                else:
                    self._json({"ok": True, "result": {}})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        # `poll_interval` bajo: `shutdown()` espera a que el bucle lo
        # note, y el medio segundo por omision se pagaba en CADA prueba
        # que levanta un servidor. Eran 48 s de los 101 que tardaba la
        # suite: mas de la mitad del tiempo, esperando a nada.
        threading.Thread(target=lambda: self._srv.serve_forever(0.01),
                         daemon=True).start()
        # Sin `sleep`: `ThreadingHTTPServer` ya hizo bind y listen en su
        # constructor, asi que el socket acepta conexiones antes de que
        # `serve_forever` arranque — se encolan en el backlog. Dormir
        # "por si acaso" aqui costaba segundos repartidos por toda la suite.
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
