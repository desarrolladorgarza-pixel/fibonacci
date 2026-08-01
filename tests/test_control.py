"""
Pruebas del control de equipo, identidad y subagentes.

Verifican el modelo de autorización sin tocar pantalla ni red reales.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import dataclasses as _dc
import subprocess as _sp

from fibonacci.control import (
    InputError, Remote, RemoteError, RemoteHost, ScreenError, _mutates_shell,
    capture, click, input_backend, press_key, scroll, type_text,
    window_is_sensitive,
)
from fibonacci.identity import (
    Authority, Decision, Principal, Trust,
)
from fibonacci.journal import Journal
from fibonacci.tools import ToolBox


# ===========================================================================
# Identidad: quién puede pedir qué
# ===========================================================================

def _auth():
    a = Authority()
    a.principals["cli:local"] = Principal("cli:local", "dueño", Trust.OWNER)
    a.principals["tg:1"] = Principal("tg:1", "amigo", Trust.MEMBER, "telegram")
    a.principals["tg:2"] = Principal("tg:2", "invitado", Trust.GUEST, "telegram")
    return a


def test_desconocido_no_puede_nada():
    """'De cualquiera' NO puede significar de cualquiera."""
    a = _auth()
    d, why = a.check(Principal("tg:999"), "file.write", "~/proyectos/x.py")
    assert d == Decision.DENY and "emparejado" in why


def test_dueno_opera_libre_en_su_ambito():
    """Autonomía real: dentro del ámbito no pregunta nada."""
    a = _auth()
    d, _ = a.check(a.principal("cli:local"), "file.write", "~/proyectos/api/main.py")
    assert d == Decision.ALLOW


def test_ambito_prohibido_no_se_negocia():
    a = _auth()
    for ruta in ("/etc/passwd", "~/.ssh/id_rsa", "/boot/grub.cfg"):
        d, _ = a.check(a.principal("cli:local"), "file.write", ruta)
        assert d == Decision.DENY, ruta


def test_deny_del_nucleo_no_se_puede_anular_por_config():
    a = _auth()
    a.add_scope("/etc/**", Decision.ALLOW, Trust.OWNER, "intento de anular")
    d, _ = a.check(a.principal("cli:local"), "file.write", "/etc/passwd")
    assert d == Decision.DENY, "las DENY del núcleo van primero, siempre"


def test_invitado_requiere_confirmacion_incluso_para_leer():
    """Mostrarle tus archivos a un invitado ya es una divulgación."""
    a = _auth()
    for accion in ("file.write", "file.read"):
        d, why = a.check(a.principal("tg:2"), accion, "~/proyectos/x.py")
        assert d == Decision.CONFIRM, accion
        assert "invitado" in why


def test_miembro_si_lee_libre_en_ambito():
    a = _auth()
    d, _ = a.check(a.principal("tg:1"), "file.read", "~/proyectos/x.py")
    assert d == Decision.ALLOW


def test_produccion_exige_confirmacion():
    a = _auth()
    d, _ = a.check(a.principal("cli:local"), "file.write", "/srv/produccion/app.conf")
    assert d == Decision.CONFIRM


def test_fuera_de_ambito_confirma_pero_no_prohibe():
    """El agente debe poder trabajar; solo no asume permiso que nadie dio."""
    a = _auth()
    d, why = a.check(a.principal("cli:local"), "file.write", "/opt/algo/x.txt")
    assert d == Decision.CONFIRM and "fuera de tus ámbitos" in why


def test_emparejamiento_con_codigo_de_un_solo_uso():
    a = _auth()
    code = a.new_pairing_code()
    assert a.pair("tg:nuevo", code, "Ana", Trust.MEMBER)
    assert a.principals["tg:nuevo"].trust == Trust.MEMBER
    assert not a.pair("tg:otro", code), "el código no debe servir dos veces"


def test_codigo_invalido_no_empareja():
    assert not _auth().pair("tg:x", "codigo-inventado-999")


def test_revocacion():
    a = _auth()
    assert a.revoke("tg:1")
    d, _ = a.check(a.principal("tg:1"), "file.write", "~/proyectos/x.py")
    assert d == Decision.DENY


def test_persistencia_de_ambitos(tmp_path):
    p = tmp_path / "authority.json"
    a = Authority(path=p)
    a.principals["cli:local"] = Principal("cli:local", "dueño", Trust.OWNER)
    a.add_scope("~/trabajo/**", Decision.ALLOW, note="mi área")
    b = Authority.load(p)
    d, _ = b.check(b.principal("cli:local"), "file.write", "~/trabajo/x.py")
    assert d == Decision.ALLOW
    assert any(s.pattern == "/etc/**" and s.decision == Decision.DENY
               for s in b.scopes), "las CORE_DENY sobreviven al guardado"


# ===========================================================================
# Pantalla: lo irreversible se trata como irreversible
# ===========================================================================

@pytest.mark.parametrize("titulo", [
    "BBVA Bancomer - Transferencias",
    "1Password",
    "Gmail - Bandeja de entrada",
    "root@servidor: ~",
    "AWS Console - EC2",
    "Checkout - Pagar ahora",
])
def test_ventanas_sensibles_detectadas(titulo):
    assert window_is_sensitive(titulo)


@pytest.mark.parametrize("titulo", [
    "main.py - Visual Studio Code",
    "Documento sin título - LibreOffice",
    "Terminal",
])
def test_ventanas_normales_no_disparan(titulo):
    assert window_is_sensitive(titulo) is None


def test_acciones_de_pantalla_son_irreversibles(tmp_path):
    """No existe un des-clic: el sistema debe decirlo, no fingir."""
    from fibonacci.tools_control import attach_screen

    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    attach_screen(box)

    for nombre in ("screen.click", "screen.type", "screen.key"):
        spec = next(s for s in box.specs() if s.name == nombre)
        assert spec.mutating and not spec.reversible, nombre

    captura = next(s for s in box.specs() if s.name == "screen.capture")
    assert not captura.mutating, "mirar no muta"


def test_backend_de_entrada_se_reporta():
    b = input_backend()
    assert isinstance(b, str) and b


# ===========================================================================
# Remoto
# ===========================================================================

def test_host_desconocido_da_error_util():
    r = Remote()
    with pytest.raises(RemoteError, match="no registrado"):
        r.get("produccion")


def test_readonly_rechaza_comandos_que_mutan():
    r = Remote({"lectura": RemoteHost("lectura", "h", "u", scope="readonly")})
    with pytest.raises(RemoteError, match="readonly"):
        r.run("lectura", "rm -rf /var/log/*")


def test_deteccion_de_comandos_mutantes():
    for cmd in ("rm -rf /tmp/x", "systemctl restart nginx", "apt install curl",
                "chmod 777 /etc/passwd", "echo x > /etc/hosts"):
        assert _mutates_shell(cmd), cmd
    for cmd in ("ls -la", "cat /var/log/syslog", "df -h", "ps aux",
                "grep error app.log", "journalctl -u nginx"):
        assert not _mutates_shell(cmd), cmd

    # Redirecciones: el caso que un \b ingenuo no atrapa
    for cmd in ("echo x > /etc/hosts", "cat a >> b", "date > /tmp/t"):
        assert _mutates_shell(cmd), cmd
    assert not _mutates_shell("cat < input.txt"), "leer de un archivo no muta"


def test_scp_recibe_el_puerto_en_P_mayuscula_y_sin_sobras():
    """
    El fallo más grave del control remoto, y solo salía usándolo contra un
    servidor de verdad.

    `scp` quiere `-P` y `ssh` quiere `-p`. Se resolvía filtrando `"-p"` de
    `ssh_args()`, lo que quitaba el flag **pero dejaba el número suelto**: scp
    lo tomaba por un archivo de origen y abortaba con `stat local "22"`. Como
    `ssh_args()` siempre incluye el puerto, `write()` y `fetch()` fallaban en
    TODOS los hosts —no solo en los de puerto raro— y con ellos el undo
    remoto, que depende de `fetch()` para guardar la copia previa.
    """
    h = RemoteHost("x", "servidor", "root", port=2222, key_file="/tmp/k")

    assert "-p" in h.ssh_args() and "2222" in h.ssh_args()

    scp = h.scp_args()
    assert "-P" in scp, "scp usa -P mayúscula"
    assert "-p" not in scp, "-p en scp significa 'preservar tiempos', no puerto"
    assert scp[scp.index("-P") + 1] == "2222"
    # Ningún argumento suelto: todo valor va precedido de su flag.
    sueltos = [a for i, a in enumerate(scp)
               if not a.startswith("-") and (i == 0 or not scp[i - 1].startswith("-"))]
    assert not sueltos, f"scp interpretaría esto como rutas: {sueltos}"


def test_el_puerto_por_defecto_tampoco_se_cuela_suelto():
    h = RemoteHost("x", "servidor", "root")      # puerto 22
    scp = h.scp_args()
    assert scp[scp.index("-P") + 1] == "22"
    assert scp.count("22") == 1, "el puerto solo debe aparecer tras su flag"


def test_escritura_remota_es_reversible(tmp_path):
    """Los archivos remotos SÍ tienen undo, a diferencia de los comandos."""
    from fibonacci.tools_control import attach_remote

    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    attach_remote(box, Remote({"stg": RemoteHost("stg", "h", "u", scope="free")}))

    w = next(s for s in box.specs() if s.name == "remote.write")
    assert w.mutating and w.reversible
    assert "remote.write" in j._undoers

    r = next(s for s in box.specs() if s.name == "remote.run")
    assert not r.reversible, "un comando remoto no se puede deshacer"


def test_un_undo_remoto_que_falla_no_se_marca_como_hecho(tmp_path):
    """
    El peor fallo posible en este producto, y solo salía contra un servidor de
    verdad: `fib undo` respondía OK, el journal daba la acción por revertida, y
    el archivo del servidor seguía cambiado.

    La causa: el undoer devolvía "fallo al restaurar" como texto de retorno, y
    `Journal._undo` toma cualquier retorno por éxito — solo una excepción deja
    la acción intacta.
    """
    from fibonacci.contracts import ActionStatus
    from fibonacci.tools_control import attach_remote

    class RemotoCaido(Remote):
        """Un servidor que no responde a nada."""

        def write(self, alias, path, content):
            return f"escrito {alias}:{path}"

        def exists(self, alias, path):
            return False

        def push(self, alias, local_path, remote_path):
            raise RemoteError("connection refused")

        def run(self, alias, command, timeout=120):
            return 255, "ssh: connect to host: Connection refused"

    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    attach_remote(box, RemotoCaido(
        {"stg": RemoteHost("stg", "h", "u", scope="free")}))

    box.invoke("remote.write", {"alias": "stg", "path": "/tmp/x.conf",
                                "content": "nuevo"}, "s1")

    ok, msg = j.undo_last("s1")
    assert not ok, "un undo que no revirtió nada no puede decir que sí"
    assert j.history("s1")[0].status is ActionStatus.APPLIED, \
        "la acción debe seguir pendiente de deshacer, no marcada como UNDONE"


def test_ambitos_por_host():
    hosts = {
        "stg": RemoteHost("stg", "s.x", "u", scope="free"),
        "prod": RemoteHost("prod", "p.x", "u", scope="confirm"),
        "audit": RemoteHost("audit", "a.x", "u", scope="readonly"),
    }
    r = Remote(hosts)
    assert r.get("stg").scope == "free"
    assert r.get("prod").scope == "confirm"


# ===========================================================================
# Subagentes
# ===========================================================================

class _FakeMesh:
    from fibonacci.mesh.registry import Catalog as _C
    catalog = _C.from_profile("local")
    mode = "local"

    class _L:
        cost_usd = 0.0
    ledger = _L()

    def ask(self, cap, messages, **k):
        from fibonacci.contracts import Completion
        return Completion(text="hecho", model="fake")

    def embed(self, texts):
        raise RuntimeError("sin embeddings")


def _parent(tmp):
    from fibonacci.agent import Agent, SpendBudget
    from fibonacci.memory import Memory

    j = Journal(tmp / "j.db", snapshots=tmp / "s")
    box = ToolBox(j, root=tmp / "ws", confirm=lambda d, x: True)
    return Agent(_FakeMesh(), Memory(tmp / "m.db"), j, box,
                 budget=SpendBudget(max_usd=4.0))


def test_subagentes_comparten_journal(tmp_path):
    """Sin esto, delegar rompe la garantía de undo del producto."""
    from fibonacci.subagents import Swarm

    padre = _parent(tmp_path)
    s = Swarm(padre, max_parallel=3)
    hijo = s._spawn("s1/sub-1", 0.5)
    assert hijo.journal is padre.journal


def test_subagente_no_hereda_contaminacion(tmp_path):
    """Que uno lea una web no debe bloquear la salida de los demás."""
    from fibonacci.subagents import Swarm

    padre = _parent(tmp_path)
    padre.tools.taint.sensitive_reads.append(".env")
    hijo = Swarm(padre)._spawn("s1/sub-1", 0.5)
    assert not hijo.tools.taint.holds_secrets
    assert hijo.tools.taint is not padre.tools.taint


def test_presupuesto_se_reparte_no_se_multiplica(tmp_path):
    """El error clásico al paralelizar: 5 hijos gastando el techo completo."""
    from fibonacci.subagents import Swarm

    padre = _parent(tmp_path)
    s = Swarm(padre)
    hijos = [s._spawn(f"s1/sub-{i}", 0.25) for i in range(4)]
    assert sum(h.budget.max_usd for h in hijos) <= padre.budget.max_usd + 1e-9


def test_subagente_no_excede_el_ambito_del_padre(tmp_path):
    from fibonacci.subagents import Swarm

    padre = _parent(tmp_path)
    hijo = Swarm(padre)._spawn("s1/sub-1", 0.5)
    assert hijo.tools.root == padre.tools.root


def test_ejecucion_en_paralelo(tmp_path):
    from fibonacci.subagents import Swarm, SubTask

    padre = _parent(tmp_path)
    res = Swarm(padre, max_parallel=3).run(
        [SubTask("a", "uno"), SubTask("b", "dos"), SubTask("c", "tres")], "s1")
    assert len(res) == 3 and all(r.ok for r in res)
    assert [r.name for r in res] == ["uno", "dos", "tres"]


def test_dependencias_se_respetan(tmp_path):
    from fibonacci.subagents import Swarm, SubTask

    padre = _parent(tmp_path)
    res = Swarm(padre).run(
        [SubTask("investiga", "buscar"),
         SubTask("escribe con lo anterior", "redactar", depends_on=["buscar"])],
        "s1")
    assert all(r.ok for r in res)


def test_dependencia_circular_no_cuelga(tmp_path):
    from fibonacci.subagents import Swarm, SubTask

    padre = _parent(tmp_path)
    res = Swarm(padre).run(
        [SubTask("x", "a", depends_on=["b"]), SubTask("y", "b", depends_on=["a"])],
        "s1")
    assert all(not r.ok for r in res)
    assert any("circular" in r.error for r in res)


def test_fallo_de_un_subagente_no_tumba_al_resto(tmp_path):
    from fibonacci.subagents import Swarm, SubTask

    padre = _parent(tmp_path)
    s = Swarm(padre)
    original = s._spawn

    def roto(session, share):
        if "malo" in session:
            raise RuntimeError("simulado")
        return original(session, share)
    s._spawn = roto

    res = s.run([SubTask("a", "bueno"), SubTask("b", "malo")], "s1")
    assert res[0].ok and not res[1].ok
    assert "simulado" in res[1].error


# ===========================================================================
# Pantalla: degradación y autorización
#
# El control de pantalla nunca se había ejercitado. No hace falta un X11 vivo
# para cubrir lo que importa: qué pasa cuando NO hay backend (el caso de
# cualquier servidor sin GUI, que es donde más se despliega esto), que cada
# camino de plataforma llame a la herramienta correcta, y que ninguna acción
# de pantalla se salte al `Authority`.
# ===========================================================================


def _sin_backend(monkeypatch):
    """Un Linux pelado: ni xdotool ni ydotool."""
    import fibonacci.control as ctl

    # `PLATFORM` es un dataclass congelado: se sustituye entero.
    monkeypatch.setattr(ctl, "PLATFORM", _dc.replace(ctl.PLATFORM, os="linux"))
    monkeypatch.setattr(ctl.shutil, "which", lambda t: None)


@pytest.mark.parametrize("accion", [
    lambda: click(10, 20),
    lambda: type_text("hola"),
    lambda: press_key("Return"),
    lambda: scroll(3),
])
def test_sin_backend_de_entrada_se_explica_como_arreglarlo(monkeypatch, accion):
    """
    Un servidor sin GUI es el destino habitual de este agente. El error tiene
    que decir qué instalar, no reventar con un `FileNotFoundError` de
    subprocess.
    """
    _sin_backend(monkeypatch)
    with pytest.raises(InputError, match="xdotool.*ydotool"):
        accion()


def test_el_backend_disponible_se_reporta_con_su_nombre(monkeypatch):
    """`fib doctor` lo enseña: si dice 'ninguno', ya sabes por qué no va."""
    import fibonacci.control as ctl

    # `PLATFORM` es un dataclass congelado: se sustituye entero.
    monkeypatch.setattr(ctl, "PLATFORM", _dc.replace(ctl.PLATFORM, os="linux"))

    monkeypatch.setattr(ctl.shutil, "which", lambda t: "/usr/bin/x" if t == "xdotool" else None)
    assert "xdotool" in input_backend()

    monkeypatch.setattr(ctl.shutil, "which", lambda t: "/usr/bin/y" if t == "ydotool" else None)
    assert "ydotool" in input_backend()

    monkeypatch.setattr(ctl.shutil, "which", lambda t: None)
    assert "ninguno" in input_backend()


def test_cada_accion_usa_el_backend_que_hay(monkeypatch):
    """Con xdotool presente, se llama a xdotool — y con los argumentos suyos."""
    import fibonacci.control as ctl

    llamadas = []
    # `PLATFORM` es un dataclass congelado: se sustituye entero.
    monkeypatch.setattr(ctl, "PLATFORM", _dc.replace(ctl.PLATFORM, os="linux"))
    monkeypatch.setattr(ctl.shutil, "which",
                        lambda t: "/usr/bin/xdotool" if t == "xdotool" else None)
    monkeypatch.setattr(ctl.subprocess, "run",
                        lambda cmd, **kw: llamadas.append(cmd) or
                        _sp.CompletedProcess(cmd, 0, b"", b""))

    assert "(10, 20)" in click(10, 20)
    assert "4 caracteres" in type_text("hola")  # no repite lo tecleado
    assert "Return" in press_key("Return")
    scroll(-3)

    assert all(c[0] == "xdotool" for c in llamadas), llamadas
    assert ["mousemove", "10", "20"] == llamadas[0][1:4]
    assert "type" in llamadas[1] and "hola" in llamadas[1]
    assert "key" in llamadas[2] and "Return" in llamadas[2]


def test_sin_forma_de_capturar_la_pantalla_se_dice(monkeypatch):
    import fibonacci.control as ctl

    # `PLATFORM` es un dataclass congelado: se sustituye entero.
    monkeypatch.setattr(ctl, "PLATFORM", _dc.replace(ctl.PLATFORM, os="linux"))
    monkeypatch.setattr(ctl.shutil, "which", lambda t: None)
    with pytest.raises(ScreenError):
        capture()


def test_las_acciones_de_pantalla_pasan_por_el_authority(tmp_path):
    """
    Ninguna acción de pantalla puede saltarse el permiso: teclear en la ventana
    de un banco es tan destructivo como un `rm -rf`, y menos evidente.
    """
    from fibonacci.tools_control import attach_screen

    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: False)
    auth = _auth()
    invitado = Principal("chat:9", "Desconocido", Trust.GUEST)
    attach_screen(box, auth, invitado)

    pantalla = [s for s in box.specs() if s.name.startswith("screen.")]
    assert pantalla, "debe registrar herramientas de pantalla"

    for spec in pantalla:
        if spec.mutating:
            assert not spec.reversible, \
                f"'{spec.name}' no se puede deshacer y debe declararlo"


def test_una_ventana_sensible_se_detecta_antes_de_teclear():
    """La lista existe para no escribir una contraseña en el sitio equivocado."""
    assert window_is_sensitive("1Password — Bóveda personal")
    assert window_is_sensitive("Banco Santander - Transferencias")
    assert not window_is_sensitive("notas.txt — VS Code")
