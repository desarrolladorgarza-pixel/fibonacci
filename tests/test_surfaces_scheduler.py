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
    DiscordSurface, Inbound, SurfaceRunner, Surface, TelegramSurface,
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
