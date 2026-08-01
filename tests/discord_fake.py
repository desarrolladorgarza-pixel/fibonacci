"""
Imitación de la API REST de Discord, para validar `DiscordSurface`.

Reproduce lo que importa del servidor real:

  · `GET /channels/{id}/messages` devuelve **del más nuevo al más viejo**, que
    es al revés de como hay que procesarlos.
  · `after={id}` entrega solo lo posterior. Si el adaptador no lo avanza,
    reprocesa los mismos mensajes en cada vuelta.
  · Los mensajes de bots traen `author.bot: true` — incluidos los del propio
    bot. Sin filtrarlos, el agente se responde a sí mismo en bucle.
  · `POST /messages` rechaza con 400 lo que pase de 2000 caracteres.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LIMITE_DISCORD = 2000


class FakeDiscord:
    def __init__(self):
        self.mensajes = {}         # canal -> [msg]  (viejo -> nuevo)
        self.enviados = []         # {channel_id, content}
        self.peticiones = []
        self._siguiente = 1

    # -- programación ----------------------------------------------------

    def mensaje(self, texto, canal="c1", user_id="7", username="ana",
                es_bot=False):
        self.mensajes.setdefault(canal, []).append({
            "id": str(self._siguiente),
            "content": texto,
            "author": {"id": user_id, "username": username, "bot": es_bot},
        })
        self._siguiente += 1
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
                partes = urlparse(self.path)
                canal = partes.path.split("/channels/")[1].split("/")[0]
                q = parse_qs(partes.query)
                after = q.get("after", [None])[0]

                msgs = list(server.mensajes.get(canal, []))
                if after:
                    msgs = [m for m in msgs if int(m["id"]) > int(after)]
                # Discord devuelve del más nuevo al más viejo.
                self._json(list(reversed(msgs)))

            def do_POST(self):
                server.peticiones.append(self.path)
                n = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                contenido = body.get("content", "")
                if len(contenido) > LIMITE_DISCORD:
                    self._json({"message": "Must be 2000 or fewer in length",
                                "code": 50035}, 400)
                    return
                canal = self.path.split("/channels/")[1].split("/")[0]
                server.enviados.append({"channel_id": canal,
                                        "content": contenido})
                self._json({"id": "999"})

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
