"""Pruebas de superficies vivas, programador y cifrado AES."""

import base64
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from fibonacci.crypto import (
    aes_available, decrypt, describe, encrypt, is_encrypted,
)
from fibonacci.identity import Authority, Principal, Trust
from fibonacci.scheduler import Job, Scheduler, next_run
from fibonacci.surfaces.live import (
    DiscordSurface, Inbound, Outbound, SurfaceRunner, Surface, TelegramSurface,
    _split, build,
)


# ===========================================================================
# Cifrado AES real
# ===========================================================================

def test_aes_disponible_y_reportado():
    assert isinstance(describe(), str)
    if aes_available():
        assert "AES" in describe()


def test_cifrado_ida_y_vuelta():
    blob = encrypt("datos privados", "clave-fuerte")
    assert decrypt(blob, "clave-fuerte") == "datos privados"
    assert "datos privados" not in blob


def test_formato_declara_algoritmo():
    d = json.loads(encrypt("x", "k"))
    assert d["enc"] in ("fib-aes-gcm-1", "fib-hmac-ctr-2")
    assert "pbkdf2" in d["kdf"]


def test_clave_incorrecta_rechazada():
    blob = encrypt("secreto", "buena")
    with pytest.raises(ValueError):
        decrypt(blob, "mala")


def test_manipulacion_detectada():
    blob = encrypt("integro", "k")
    d = json.loads(blob)
    raw = bytearray(base64.b64decode(d["data"]))
    raw[0] ^= 0xFF
    d["data"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(ValueError):
        decrypt(json.dumps(d), "k")


def test_modo_respaldo_funciona_sin_cryptography():
    blob = encrypt("sin aes", "k", force_fallback=True)
    assert json.loads(blob)["enc"] == "fib-hmac-ctr-2"
    assert decrypt(blob, "k") == "sin aes"
    assert "_aviso" in json.loads(blob), "el modo débil debe avisarlo"


def test_lee_formato_de_version_anterior():
    """Un paquete exportado con 0.4.0 debe seguir abriéndose."""
    import hashlib

    salt = b"S" * 16
    key = hashlib.pbkdf2_hmac("sha256", b"vieja", salt, 600_000, dklen=32)
    data = b"legado"
    ks = b""
    c = 0
    while len(ks) < len(data):
        ks += hashlib.sha256(key + c.to_bytes(8, "big")).digest()
        c += 1
    cipher = bytes(a ^ b for a, b in zip(data, ks))
    viejo = json.dumps({
        "enc": "fib-xor-1", "salt": base64.b64encode(salt).decode(),
        "mac": base64.b64encode(hashlib.sha256(key + cipher).digest()[:16]).decode(),
        "data": base64.b64encode(cipher).decode()})
    assert decrypt(viejo, "vieja") == "legado"


def test_deteccion_de_archivo_cifrado():
    assert is_encrypted(encrypt("x", "k"))
    assert not is_encrypted('{"notes": []}')
    assert not is_encrypted("texto plano")


# ===========================================================================
# Programador: horarios
# ===========================================================================

BASE = datetime(2026, 8, 1, 10, 30).timestamp()   # sábado


@pytest.mark.parametrize("expr,esperado", [
    ("cada 30m", "2026-08-01 11:00"),
    ("cada 2h", "2026-08-01 12:30"),
    ("diario 07:00", "2026-08-02 07:00"),
    ("lunes 09:30", "2026-08-03 09:30"),
    ("0 7 * * *", "2026-08-02 07:00"),
    ("*/15 * * * *", "2026-08-01 10:45"),
])
def test_horarios_naturales_y_cron(expr, esperado):
    t = next_run(expr, BASE)
    assert datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") == esperado


def test_horario_invalido_falla_al_crear_no_al_correr():
    with pytest.raises(ValueError, match="no reconocido"):
        next_run("cuando me acuerde")


def test_diario_ya_pasado_salta_al_dia_siguiente():
    t = next_run("diario 07:00", BASE)          # son las 10:30
    assert datetime.fromtimestamp(t).day == 2


# ===========================================================================
# Programador: ciclo de vida
# ===========================================================================

class _Agent:
    class _T:
        confirm = None
    class _B:
        max_usd = 2.0
    tools = _T()
    budget = _B()

    def __init__(self, falla=False, pide_confirmacion=False):
        self.falla = falla
        self.pide = pide_confirmacion

    def chat(self, text, session, surface=None):
        if self.falla:
            raise RuntimeError("modelo caído")
        if self.pide and self.tools.confirm:
            self.tools.confirm("borrar algo peligroso", 2)
        class R:
            text = "resultado"
            cost_usd = 0.02
            actions = []
        return R()


def test_alta_y_listado(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    s.add(Job(name="diaria", instruction="x", schedule="diario 07:00"))
    assert len(s.list()) == 1
    assert s.get("diaria") is not None


def test_pendientes_por_tiempo(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    s.add(Job(name="j", instruction="x", schedule="diario 07:00"))
    assert not s.due()
    assert len(s.due(time.time() + 2 * 86400)) == 1


def test_desactivada_no_se_ejecuta(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    s.add(Job(name="j", instruction="x", schedule="cada 1h"))
    s.toggle("j", False)
    assert not s.due(time.time() + 86400)


def test_ejecucion_registra_y_reprograma(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    j = s.add(Job(name="j", instruction="x", schedule="cada 1h"))
    antes = j.next_run
    r = s.execute(j, _Agent())
    assert r["ok"]
    despues = s.get("j")
    assert despues.runs == 1
    # No se compara con `>` a secas: `antes` y la reprogramación se toman con
    # microsegundos de diferencia, y el reloj de Windows avanza a saltos de
    # ~15 ms, así que ambos caían en el mismo tick y la prueba fallaba sin que
    # nada estuviera mal. Lo que de verdad promete "cada 1h" es esto:
    assert despues.next_run >= antes, "la próxima ejecución nunca retrocede"
    assert abs(despues.next_run - (time.time() + 3600)) < 5, "reprogramada ~1h"
    assert len(s.history(j.id)) == 1


def test_tarea_desatendida_no_confirma_nada(tmp_path):
    """Nadie está mirando: lo que exigiría confirmación se salta y se reporta."""
    s = Scheduler(tmp_path / "s.db")
    j = s.add(Job(name="j", instruction="x", schedule="cada 1h"))
    r = s.execute(j, _Agent(pide_confirmacion=True))
    assert r["omitidas"] == 1
    assert "confirmación" in r["texto"]


def test_presupuesto_se_aplica_y_se_restaura(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    j = s.add(Job(name="j", instruction="x", schedule="cada 1h", budget_usd=0.1))
    a = _Agent()
    original = a.budget.max_usd
    s.execute(j, a)
    assert a.budget.max_usd == original, "debe restaurar el presupuesto del agente"


def test_cinco_fallos_desactivan_la_tarea(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    s.add(Job(name="rota", instruction="x", schedule="cada 1h"))
    for _ in range(5):
        s.execute(s.get("rota"), _Agent(falla=True))
    assert not s.get("rota").enabled


def test_exito_reinicia_el_contador_de_fallos(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    s.add(Job(name="j", instruction="x", schedule="cada 1h"))
    for _ in range(3):
        s.execute(s.get("j"), _Agent(falla=True))
    assert s.get("j").failures == 3
    s.execute(s.get("j"), _Agent())
    assert s.get("j").failures == 0 and s.get("j").enabled


def test_entrega_a_superficie(tmp_path):
    s = Scheduler(tmp_path / "s.db")
    j = s.add(Job(name="j", instruction="x", schedule="cada 1h",
                  surface="telegram", channel="123"))
    entregas = []
    s.execute(j, _Agent(), deliver=lambda job, txt: entregas.append(txt))
    assert len(entregas) == 1


# ===========================================================================
# Superficies
# ===========================================================================

class _Fake(Surface):
    name = "fake"

    def __init__(self, mensajes):
        self.mensajes = mensajes
        self.enviados = []

    def receive(self):
        yield from self.mensajes

    def send(self, channel_id, out):
        self.enviados.append(out.text)


class _AgentSurf:
    class _T:
        confirm = None
    class _B:
        max_usd = 2.0
    tools = _T()
    budget = _B()

    def __init__(self, pide=False):
        self.pide = pide
        self.recibidos = []

    def chat(self, text, session, surface=None):
        self.recibidos.append((text, session))
        if self.pide and self.tools.confirm:
            self.tools.confirm("acción peligrosa", 2)
        class R:
            text = "respuesta"
            cost_usd = 0.0
            actions = []
        return R()


def _auth_vacia():
    a = Authority()
    a.principals.clear()
    a.path = None            # no persistir en pruebas
    return a


def test_desconocido_no_ejecuta_nada():
    """La regla que hace desplegable una superficie pública."""
    surf = _Fake([Inbound("borra todos mis archivos", "999", "chat1")])
    agent = _AgentSurf()
    SurfaceRunner(agent, surf, _auth_vacia()).run()
    assert not agent.recibidos, "el agente no debió recibir nada"
    assert "no estás autorizado" in surf.enviados[0]


def test_emparejamiento_por_codigo():
    auth = _auth_vacia()
    code = auth.new_pairing_code()
    surf = _Fake([Inbound(code, "555", "chat1", display="Ana"),
                  Inbound("hola", "555", "chat1")])
    agent = _AgentSurf()
    SurfaceRunner(agent, surf, auth).run()
    assert "emparejado" in surf.enviados[0]
    assert len(agent.recibidos) == 1, "solo el segundo mensaje llega al agente"


def test_miembro_conversa_normal():
    auth = _auth_vacia()
    auth.principals["fake:7"] = Principal("fake:7", "Ana", Trust.MEMBER)
    surf = _Fake([Inbound("qué hora es", "7", "c1")])
    agent = _AgentSurf()
    SurfaceRunner(agent, surf, auth).run()
    assert agent.recibidos[0][0] == "qué hora es"
    assert "respuesta" in surf.enviados[0]


def test_confirmacion_remota_se_rechaza_y_se_explica():
    """Nadie puede dar un 'sí' informado por chat: se rechaza y se dice."""
    auth = _auth_vacia()
    auth.principals["fake:7"] = Principal("fake:7", "Ana", Trust.MEMBER)
    surf = _Fake([Inbound("borra la carpeta", "7", "c1")])
    agent = _AgentSurf(pide=True)
    SurfaceRunner(agent, surf, auth).run()
    assert "confirmación" in surf.enviados[0]
    assert "terminal" in surf.enviados[0]


def test_confirm_original_se_restaura():
    auth = _auth_vacia()
    auth.principals["fake:7"] = Principal("fake:7", "A", Trust.MEMBER)
    agent = _AgentSurf()
    def sentinel(d, x):
        return True

    agent.tools.confirm = sentinel
    SurfaceRunner(agent, _Fake([Inbound("hola", "7", "c1")]), auth).run()
    assert agent.tools.confirm is sentinel


def test_sesion_por_canal():
    auth = _auth_vacia()
    auth.principals["fake:7"] = Principal("fake:7", "A", Trust.MEMBER)
    agent = _AgentSurf()
    SurfaceRunner(agent, _Fake([Inbound("a", "7", "c1"), Inbound("b", "7", "c2")]),
                  auth).run()
    assert agent.recibidos[0][1] != agent.recibidos[1][1]


def test_sesion_compartida_unifica_contexto():
    """Empiezas en la terminal y sigues en el teléfono."""
    auth = _auth_vacia()
    auth.principals["fake:7"] = Principal("fake:7", "A", Trust.MEMBER)
    agent = _AgentSurf()
    SurfaceRunner(agent, _Fake([Inbound("a", "7", "c1")]), auth,
                  shared_session="principal").run()
    assert agent.recibidos[0][1] == "principal"


def test_particion_de_mensajes_largos_respeta_lineas():
    texto = "\n".join(f"linea {i} " + "x" * 50 for i in range(200))
    partes = _split(texto, 1000)
    assert len(partes) > 1
    assert all(len(p) <= 1000 for p in partes)
    assert "".join(partes).replace("\n", "") == texto.replace("\n", "")


def test_mensaje_corto_no_se_parte():
    assert _split("hola", 4000) == ["hola"]


def test_telegram_exige_token():
    with pytest.raises(ValueError, match="TELEGRAM"):
        TelegramSurface("")


def test_discord_exige_token():
    with pytest.raises(ValueError, match="DISCORD"):
        DiscordSurface("", [])


def test_superficie_desconocida():
    with pytest.raises(ValueError, match="desconocida"):
        build("myspace")


def test_claves_de_principal_por_superficie():
    t = TelegramSurface("token-x")
    assert t.principal_id(Inbound("x", "123")) == "telegram:123"
    assert t.session_key(Inbound("x", "123", "chat9")) == "telegram:chat9"


# ===========================================================================
# Telegram contra una imitación fiel de su Bot API
#
# Hasta ahora `TelegramSurface` nunca había hablado con nada: el README lo
# ofrece como forma de usar el agente desde el teléfono, y era de lo poco que
# el propio proyecto listaba como "sin verificar". `telegram_fake` reproduce
# el comportamiento del servidor real en lo que importa —semántica de
# `offset`, el tope de 4096 en `sendMessage`, la forma real de los updates—
# así que el camino completo se ejercita por HTTP sin salir a internet.
#
# Se espera por condición, no con `sleep` fijos: el servidor falso responde en
# milisegundos y dormir "por si acaso" es lo que convierte una suite de un
# minuto en una de tres.
# ===========================================================================

def _telegram(tg):
    """Una superficie apuntando al servidor falso."""
    s = TelegramSurface("token-de-prueba", poll_timeout=1)
    s.base = f"{tg.base_url}/bottoken-de-prueba"
    return s


def _hasta(condicion, limite=10.0):
    """Espera activa hasta que se cumpla algo, o se acaba el plazo."""
    fin = time.time() + limite
    while time.time() < fin:
        if condicion():
            return True
        time.sleep(0.02)
    return False


def _escuchar(surface, condicion):
    """Corre el long polling hasta que `condicion(recibidos)` se cumpla."""
    import threading

    recibidos = []
    hilo = threading.Thread(
        target=lambda: [recibidos.append(i) for i in surface.receive()],
        daemon=True)
    hilo.start()
    _hasta(lambda: condicion(recibidos))
    surface.stop()
    hilo.join(timeout=5)
    return recibidos


def _correr_runner(surface, agent, auth, condicion):
    import threading

    threading.Thread(target=SurfaceRunner(agent, surface, auth).run,
                     daemon=True).start()
    _hasta(condicion)
    surface.stop()


@pytest.fixture
def telegram():
    from telegram_fake import FakeTelegram

    tg = FakeTelegram().start()
    yield tg
    tg.stop()


def test_telegram_recibe_mensajes_y_los_traduce(telegram):
    telegram.mensaje("hola bot", user_id="7", chat_id="c1", username="ana")
    recibidos = _escuchar(_telegram(telegram), lambda r: len(r) >= 1)

    assert len(recibidos) == 1
    i = recibidos[0]
    assert i.text == "hola bot"
    assert i.user_id == "7" and i.channel_id == "c1" and i.display == "ana"


def test_telegram_avanza_el_offset_y_no_repite(telegram):
    """
    Si el `offset` no avanza, el bot vuelve a procesar el mismo mensaje en
    cada sondeo — para siempre. Es el fallo clásico de un bot de Telegram.
    """
    telegram.mensaje("uno").mensaje("dos")

    def confirmados():
        return [p.split("offset=")[1].split("&")[0]
                for p in telegram.peticiones if "offset=" in p]

    recibidos = _escuchar(_telegram(telegram),
                          lambda r: len(r) >= 2 and len(confirmados()) >= 2)

    assert [i.text for i in recibidos] == ["uno", "dos"], "entregó de más o de menos"
    offsets = confirmados()
    assert offsets[0] == "0", "el primer sondeo pide desde el principio"
    assert offsets[-1] == "3", "tras confirmar 1 y 2, el offset debe ser 3"


def test_telegram_ignora_lo_que_no_es_texto(telegram):
    """Una encuesta o un evento sin texto no debe llegar al agente."""
    telegram.evento_sin_texto()
    telegram.mensaje("esto sí", como_caption=True)
    recibidos = _escuchar(_telegram(telegram), lambda r: len(r) >= 1)

    assert [i.text for i in recibidos] == ["esto sí"], \
        "el caption cuenta como texto; la encuesta no"


def test_telegram_parte_los_mensajes_largos(telegram):
    """Telegram rechaza con 400 cualquier texto de más de 4096 caracteres."""
    largo = "\n".join(f"linea {i} " + "x" * 80 for i in range(120))
    assert len(largo) > 4096

    _telegram(telegram).send("c1", Outbound(largo))

    assert len(telegram.enviados) > 1, "debió partirse"
    assert all(len(e["text"]) <= 4096 for e in telegram.enviados), \
        "el servidor real habría devuelto 400"
    assert all(e["chat_id"] == "c1" for e in telegram.enviados)
    # Se parte por líneas: ningún trozo empieza a mitad de una.
    assert all(not e["text"].startswith("x") for e in telegram.enviados)
    recompuesto = "".join(e["text"] for e in telegram.enviados)
    assert "linea 0" in recompuesto and "linea 119" in recompuesto


def test_telegram_un_desconocido_no_llega_al_agente(telegram):
    """La promesa que hace desplegable un bot público."""
    telegram.mensaje("borra todos mis archivos", user_id="999")
    surface = _telegram(telegram)
    agent = _AgentSurf()

    _correr_runner(surface, agent, _auth_vacia(), lambda: telegram.enviados)

    assert not agent.recibidos, "el agente no debió ver nada"
    assert telegram.enviados, "pero sí se le respondió con cortesía"
    assert "no estás autorizado" in telegram.enviados[0]["text"]


def test_telegram_un_emparejado_si_llega_al_agente(telegram):
    telegram.mensaje("¿cómo va el servidor?", user_id="7")
    auth = _auth_vacia()
    auth.principals["telegram:7"] = Principal("telegram:7", "Ana", Trust.MEMBER)

    surface = _telegram(telegram)
    agent = _AgentSurf()
    _correr_runner(surface, agent, auth, lambda: telegram.enviados)

    assert [t for t, _ in agent.recibidos] == ["¿cómo va el servidor?"]
    assert telegram.enviados and "respuesta" in telegram.enviados[0]["text"]


def test_telegram_lo_que_exige_confirmacion_se_rechaza_y_se_explica(telegram):
    """Por chat nadie puede dar un sí informado: se omite y se dice dónde."""
    telegram.mensaje("borra la base de datos", user_id="7")
    auth = _auth_vacia()
    auth.principals["telegram:7"] = Principal("telegram:7", "Ana", Trust.MEMBER)

    surface = _telegram(telegram)
    _correr_runner(surface, _AgentSurf(pide=True), auth,
                   lambda: telegram.enviados)

    texto = "".join(e["text"] for e in telegram.enviados)
    assert "confirmación" in texto
    assert "terminal" in texto.lower(), "debe decir dónde sí puede hacerse"


# ===========================================================================
# Discord contra una imitación de su API REST
# ===========================================================================

def _discord(fd, canales=("c1",)):
    s = DiscordSurface("token-de-prueba", list(canales), interval=0.05)
    s.base = fd.base_url
    return s


@pytest.fixture
def discord():
    from discord_fake import FakeDiscord

    fd = FakeDiscord().start()
    yield fd
    fd.stop()


def test_discord_recibe_y_traduce(discord):
    discord.mensaje("hola", canal="c1", user_id="7", username="ana")
    recibidos = _escuchar(_discord(discord), lambda r: len(r) >= 1)

    assert len(recibidos) == 1
    i = recibidos[0]
    assert i.text == "hola" and i.user_id == "7" and i.channel_id == "c1"
    assert i.display == "ana"


def test_discord_ignora_a_los_bots(discord):
    """
    Discord devuelve también los mensajes del propio bot. Si no se filtran, el
    agente lee su propia respuesta, la contesta, y se responde a sí mismo para
    siempre — gastando presupuesto en cada vuelta.
    """
    discord.mensaje("respuesta anterior del bot", es_bot=True, username="fibo")
    discord.mensaje("hola de verdad", user_id="7")
    recibidos = _escuchar(_discord(discord), lambda r: len(r) >= 1)

    assert [i.text for i in recibidos] == ["hola de verdad"]


def test_discord_los_procesa_del_mas_viejo_al_mas_nuevo(discord):
    """La API los devuelve al revés; el orden de una conversación importa."""
    discord.mensaje("primero").mensaje("segundo").mensaje("tercero")
    recibidos = _escuchar(_discord(discord), lambda r: len(r) >= 3)

    assert [i.text for i in recibidos] == ["primero", "segundo", "tercero"]


def test_discord_avanza_after_y_no_repite(discord):
    """Sin `after`, cada vuelta reprocesa lo mismo."""
    discord.mensaje("uno").mensaje("dos")

    def con_after():
        return [p for p in discord.peticiones if "after=" in p]

    recibidos = _escuchar(_discord(discord),
                          lambda r: len(r) >= 2 and con_after())

    assert [i.text for i in recibidos] == ["uno", "dos"], "no debe repetir"
    assert con_after(), "la segunda vuelta debe pedir solo lo posterior"
    assert "after=2" in con_after()[-1]


def test_discord_ignora_mensajes_vacios(discord):
    """Una imagen sin texto no tiene nada que responder."""
    discord.mensaje("", user_id="7")
    discord.mensaje("esto sí", user_id="7")
    recibidos = _escuchar(_discord(discord), lambda r: len(r) >= 1)

    assert [i.text for i in recibidos] == ["esto sí"]


def test_discord_varios_canales(discord):
    discord.mensaje("desde c1", canal="c1")
    discord.mensaje("desde c2", canal="c2")
    recibidos = _escuchar(_discord(discord, ("c1", "c2")), lambda r: len(r) >= 2)

    assert {i.channel_id for i in recibidos} == {"c1", "c2"}


def test_discord_parte_por_debajo_del_tope(discord):
    """Discord rechaza con 400 lo que pase de 2000 caracteres."""
    largo = "\n".join(f"linea {i} " + "y" * 60 for i in range(80))
    assert len(largo) > 2000

    _discord(discord).send("c1", Outbound(largo))

    assert len(discord.enviados) > 1, "debió partirse"
    assert all(len(e["content"]) <= 2000 for e in discord.enviados), \
        "el servidor real habría devuelto 400"
    assert all(e["channel_id"] == "c1" for e in discord.enviados)


def test_discord_un_desconocido_no_llega_al_agente(discord):
    discord.mensaje("borra la base de datos", user_id="999")
    surface = _discord(discord)
    agent = _AgentSurf()

    _correr_runner(surface, agent, _auth_vacia(), lambda: discord.enviados)

    assert not agent.recibidos
    assert discord.enviados and "no estás autorizado" in discord.enviados[0]["content"]


# ===========================================================================
# El contrato de extensión
# ===========================================================================

def test_el_contrato_publicado_es_el_que_usa_el_runtime():
    """
    `surfaces/base.py` documenta cómo escribir una superficie nueva, y el
    README lo vende como "un archivo, no un parche al gateway".

    Declaraba sus propios `Inbound`/`Outbound`/`Surface`, distintos de los que
    `live.py` usa de verdad: nadie lo importaba, y a su `Inbound` le faltaba
    `display`, que `SurfaceRunner` sí lee. Seguir el contrato publicado
    producía una superficie que reventaba en cuanto llegara un mensaje.
    """
    from fibonacci.surfaces import base, live

    assert base.Inbound is live.Inbound
    assert base.Outbound is live.Outbound
    assert base.Surface is live.Surface


def test_una_superficie_escrita_contra_el_contrato_funciona():
    """La prueba de que el contrato publicado sirve: se escribe una superficie
    importando solo de `base` y `SurfaceRunner` la conduce sin tocarla."""
    from fibonacci.surfaces.base import Inbound, Outbound, Surface, SurfaceRunner

    class MiPlataforma(Surface):
        name = "mi-plataforma"

        def __init__(self):
            self.enviados = []

        def receive(self):
            yield Inbound("hola", user_id="7", channel_id="c1", display="Ana")

        def send(self, channel_id, out: Outbound):
            self.enviados.append(out.text)

    auth = _auth_vacia()
    auth.principals["mi-plataforma:7"] = Principal(
        "mi-plataforma:7", "Ana", Trust.MEMBER)

    surf = MiPlataforma()
    agent = _AgentSurf()
    SurfaceRunner(agent, surf, auth).run()

    assert [t for t, _ in agent.recibidos] == ["hola"]
    assert surf.enviados == ["respuesta"]
    # Y los valores por defecto del contrato siguen ahí.
    assert surf.session_key(Inbound("x", "7", "c1")) == "mi-plataforma:c1"
    assert surf.principal_id(Inbound("x", "7")) == "mi-plataforma:7"
