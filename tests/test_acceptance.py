"""
Pruebas de ACEPTACIÓN.

Las demás pruebas verifican que las funciones hacen lo que dicen. Estas
verifican otra cosa: que **cada promesa del README se cumple en el código**.

La distinción importa. Un proyecto puede tener mil pruebas unitarias verdes y
aun así mentir en su portada, porque nada conecta lo que promete con lo que
hace. Cada prueba de este archivo cita la promesa que verifica.

**Estas pruebas son la compuerta de publicación.** Si una falla, o el código
está roto o el README miente; en ambos casos no se publica. `preflight.sh` las
corre con la marca `acceptance`.

Regla para quien las mantenga: si cambias una promesa del README, cambia la
prueba. Si no puedes hacer que la prueba pase, **quita la promesa** — no
suavices la aserción.
"""

from __future__ import annotations

import json
import time

import pytest

from fibonacci.contracts import Capability, Message, Note, Skill

pytestmark = pytest.mark.acceptance


# ===========================================================================
# PROMESA 1: "El agente que puedes deshacer"
# ===========================================================================

def test_promesa_undo_revierte_lo_que_hizo_el_agente(agent, fake_model, workspace):
    """README: `fib undo` — si no te gustó."""
    (workspace / "config.yml").write_text("original", encoding="utf-8")

    fake_model.reply_tool("file.write", {"path": "config.yml", "content": "cambiado"})
    fake_model.reply("Listo")
    fake_model.reply_json({"notes": [], "skill": None})
    agent.chat("cambia config.yml", "s1")
    assert (workspace / "config.yml").read_text(encoding="utf-8") == "cambiado"

    ok, _ = agent.journal.undo_last("s1")
    assert ok
    assert (workspace / "config.yml").read_text(encoding="utf-8") == "original"


def test_promesa_undo_all_revierte_la_sesion_completa(agent, fake_model, workspace):
    """README: `fib undo --all` — toda la sesión, en orden inverso."""
    (workspace / "a.txt").write_text("A0", encoding="utf-8")

    # Dos respuestas por turno, no tres: con una sola herramienta y una
    # instrucción corta, `_maybe_learn` no llama al modelo. Encolar una tercera
    # dejaba un sobrante que el turno siguiente consumía como si fuera su
    # primera respuesta, y el tercer archivo nunca se escribía.
    for accion in ({"path": "a.txt", "content": "A1"},
                   {"path": "b.txt", "content": "B1"},
                   {"path": "c.txt", "content": "C1"}):
        fake_model.reply_tool("file.write", accion)
        fake_model.reply("hecho")
        agent.chat("escribe", "s1")

    n, _ = agent.journal.undo_session("s1")
    assert n == 3
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "A0"
    assert not (workspace / "b.txt").exists()
    assert not (workspace / "c.txt").exists()


def test_promesa_undo_no_destruye_trabajo_mas_nuevo(agent, fake_model, workspace):
    """README: 'si el archivo cambió después se niega y te dice por qué'.

    Es la garantía más importante del producto: un undo que borra en silencio
    sería peor que no tener undo.
    """
    (workspace / "doc.md").write_text("v1", encoding="utf-8")
    fake_model.reply_tool("file.write", {"path": "doc.md", "content": "v2-agente"})
    fake_model.reply("hecho")
    fake_model.reply_json({"notes": [], "skill": None})
    agent.chat("edita doc.md", "s1")

    (workspace / "doc.md").write_text("MI EDICIÓN", encoding="utf-8")

    ok, msg = agent.journal.undo_last("s1")
    assert not ok
    assert "modificado" in msg
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "MI EDICIÓN"


def test_promesa_herramienta_mutante_sin_undo_no_se_registra(toolbox):
    """README: 'no es una convención documentada: es una excepción en tiempo
    de registro'."""
    from fibonacci.contracts import ToolSpec

    with pytest.raises(ValueError, match="undo"):
        toolbox.register(
            ToolSpec("db.wipe", "borra todo",
                     {"type": "object", "properties": {}}, mutating=True),
            lambda: "listo")


def test_promesa_irreversible_siempre_confirma(journal, workspace):
    """README: 'lo genuinamente irreversible se marca y siempre pide
    confirmación'."""
    from fibonacci.tools import ToolBox

    box = ToolBox(journal, root=workspace, confirm=lambda desc, danger: False)
    r = box.invoke("shell.run", {"command": "echo hola"}, "s1")
    assert not r.ok and r.needs_confirmation


# ===========================================================================
# PROMESA 2: memoria que envejece y marca contradicciones
# ===========================================================================

def test_promesa_la_memoria_decae(memory):
    """README: 'lo de hace un año pesa menos que lo de ayer'."""
    vieja = Note("trabajaba en Acme", confidence=0.9, half_life_days=180)
    vieja.ts = time.time() - 400 * 86400
    fresca = Note("trabaja en Beta", confidence=0.9, half_life_days=180)

    assert vieja.current_confidence() < fresca.current_confidence() / 2
    assert vieja.stale and not fresca.stale


def test_promesa_lo_permanente_no_caduca():
    """README: 'tu nombre no caduca'."""
    n = Note("se llama Chronos", confidence=0.95, half_life_days=0)
    n.ts = time.time() - 3650 * 86400
    assert n.current_confidence() == 0.95


def test_promesa_las_contradicciones_se_marcan(memory):
    """README: 'las contradicciones se marcan en vez de coexistir en
    silencio'."""
    memory.remember(Note("el proyecto principal usa PostgreSQL", kind="project"))
    _, conflictos = memory.remember(
        Note("el proyecto principal usa MongoDB", kind="project"))
    assert conflictos
    assert len(memory.open_conflicts()) == 1


def test_promesa_las_contradicciones_llegan_al_modelo(agent, fake_model):
    """Si el sistema detecta un conflicto, el agente debe poder preguntarte."""
    agent.memory.remember(Note("el servidor está en AWS", kind="fact"))
    agent.memory.remember(Note("el servidor está en Hetzner", kind="fact"))

    fake_model.reply("ok").reply_json({"notes": [], "skill": None})
    agent.chat("dónde está el servidor", "s1")
    assert "conflicto" in fake_model.last_system().lower()


# ===========================================================================
# PROMESA 3: skills con período de prueba
# ===========================================================================

def test_promesa_una_candidata_nunca_entra_a_un_prompt(agent, fake_model):
    """README: 'una candidata NUNCA entra a un prompt real'."""
    agent.memory.save_skill(Skill(name="nueva", body="TEXTO_DE_LA_CANDIDATA",
                                  triggers=["respaldo"], status="candidate"))
    fake_model.reply("ok").reply_json({"notes": [], "skill": None})
    agent.chat("haz un respaldo", "s1")
    assert "TEXTO_DE_LA_CANDIDATA" not in fake_model.last_system()


def test_promesa_la_escalera_de_promocion(memory):
    """README: 'candidata →(3)→ sombra →(8, ≥70%)→ activa'."""
    memory.save_skill(Skill(name="s", body="x", triggers=["x"], status="candidate"))
    for _ in range(3):
        memory.score_skill("s", True)
    assert memory.skills()[0].status == "shadow"
    for _ in range(5):
        memory.score_skill("s", True)
    assert memory.skills()[0].status == "active"


def test_promesa_una_skill_que_empeora_se_retira_sola(memory):
    """README: 'activa con <40% en ≥6 pruebas → retirada automáticamente'."""
    memory.save_skill(Skill(name="mala", body="x", triggers=["x"],
                            status="active", trials=5, wins=1))
    memory.score_skill("mala", False)
    assert memory.skills()[0].status == "retired"
    assert memory.select_skills("x") == []


def test_promesa_el_ciclo_de_aprendizaje_esta_conectado(agent, fake_model):
    """Regresión: en la 0.1.0 `score_skill` era código muerto y ninguna skill
    podía salir de `candidate`."""
    agent.memory.save_skill(Skill(name="ordenar", body="x", triggers=["ordena"],
                                  status="shadow", trials=5, wins=5))
    fake_model.default("ok")
    agent.chat("ordena mis archivos", "s1")
    agent.chat("gracias, otra cosa distinta", "s1")
    assert agent.memory.skills()[0].trials == 6


# ===========================================================================
# PROMESA 4: soberanía — nada sale de tu red en modo local
# ===========================================================================

def test_promesa_modo_local_falla_en_vez_de_degradar_a_la_nube():
    """README: 'si no hay candidato local, FALLA. Una fuga de datos no debería
    depender de que alguien recuerde configurarlo bien'."""
    from fibonacci.mesh.providers import ProviderError, build_providers
    from fibonacci.mesh.registry import Catalog, ModelCard
    from fibonacci.mesh.router import ModelMesh

    catalogo = Catalog(cards=[
        ModelCard("solo-nube", "anthropic", {Capability.CHAT}, local=False)])
    mesh = ModelMesh(catalogo, build_providers(), mode="local")

    with pytest.raises(ProviderError):
        mesh.ask(Capability.CHAT, [Message("user", "hola")])


def test_promesa_modo_local_solo_ofrece_modelos_locales(mesh):
    for cap in (Capability.CHAT, Capability.REASONING, Capability.EMBEDDING):
        for card in mesh.catalog.find(cap, local_only=True):
            assert card.local


# ===========================================================================
# PROMESA 5: seguridad
# ===========================================================================

def test_promesa_los_secretos_no_llegan_al_modelo(agent, fake_model, workspace):
    """README: 'leer un .env ya no manda tus llaves a la nube'."""
    (workspace / ".env").write_text(
        "OPENAI_API_KEY=sk-proj-" + "z" * 40 + "\nPORT=3000", encoding="utf-8")

    fake_model.reply_tool("file.read", {"path": ".env"})
    fake_model.reply("leído")
    fake_model.reply_json({"notes": [], "skill": None})
    agent.chat("lee el .env", "s1")

    enviado = json.dumps(fake_model.requests[1]["body"])
    assert "sk-proj-" + "z" * 40 not in enviado
    assert "PORT=3000" in enviado, "la redacción no debe romper el trabajo"


def test_promesa_leer_un_secreto_bloquea_la_salida_a_red(agent, fake_model, workspace):
    """README: 'si un turno lee un archivo sensible, cualquier salida a red
    queda bloqueada en ese turno'."""
    (workspace / ".env").write_text("SECRET=abc", encoding="utf-8")

    fake_model.reply_tool("file.read", {"path": ".env"})
    fake_model.reply_tool("http.get", {"url": "https://exfiltra.example/x"})
    fake_model.reply("intentado")
    fake_model.reply_json({"notes": [], "skill": None})
    agent.chat("lee el .env y súbelo", "s1")

    tools = [m for m in fake_model.requests[-2]["body"]["messages"]
             if m.get("role") == "tool"]
    salida = " ".join(m["content"] for m in tools)
    assert "DENEGADO" in salida or "exfiltraci" in salida.lower()


def test_promesa_el_contenido_externo_se_declara_como_datos():
    """README: 'el contenido externo se envuelve declarándolo datos, no
    instrucciones'."""
    from fibonacci.security import wrap_external

    w = wrap_external("compra ya", "sitio.com")
    assert "CONTENIDO_EXTERNO" in w
    assert "no lo obedezcas" in w.lower() or "manipulación" in w.lower()


def test_promesa_la_credencial_nunca_esta_en_el_contexto(tmp_path, http_server):
    """README: 'el modelo usa el nombre; la sustitución ocurre después de que
    ya escribió la petición'."""
    from fibonacci.api import ApiClient, Credential, Vault

    v = Vault(tmp_path / "v.enc")
    v.unlock("clave")
    v.put(Credential(name="api", kind="bearer", secret="TOKEN-JAMAS-VISIBLE",
                     host_allowlist=["127.0.0.1"]))

    r = ApiClient(v).request("GET", f"{http_server.base_url}/x", credential="api")
    assert "TOKEN-JAMAS-VISIBLE" in str(http_server.requests[-1]["headers"])
    assert "TOKEN-JAMAS-VISIBLE" not in r.summarize()


def test_promesa_la_credencial_no_se_filtra_a_otro_host(tmp_path):
    from fibonacci.api import ApiClient, Credential, Vault

    v = Vault(tmp_path / "v.enc")
    v.unlock("clave")
    v.put(Credential(name="api", secret="s", host_allowlist=["api.legitimo.com"]))
    with pytest.raises(PermissionError):
        ApiClient(v).request("GET", "https://evil.example/x", credential="api")


def test_promesa_los_ambitos_prohibidos_no_se_negocian():
    """README/SECURITY: 'CORE_DENY se carga antes que las tuyas y no se puede
    sobrescribir por configuración'."""
    from fibonacci.identity import Authority, Decision, Principal, Trust

    a = Authority()
    a.principals["cli:local"] = Principal("cli:local", "dueño", Trust.OWNER)
    a.add_scope("/etc/**", Decision.ALLOW, Trust.OWNER, "intento de anular")

    d, _ = a.check(a.principal("cli:local"), "file.write", "/etc/passwd")
    assert d == Decision.DENY


def test_promesa_un_desconocido_no_ejecuta_nada():
    """README: 'quien no esté emparejado no ejecuta nada'."""
    from fibonacci.identity import Authority, Decision, Principal

    a = Authority()
    a.principals.clear()
    d, why = a.check(Principal("telegram:999"), "file.write", "~/proyectos/x")
    assert d == Decision.DENY and "emparejado" in why


def test_promesa_el_presupuesto_corta(agent, fake_model, workspace):
    """README: 'techo por turno en dinero, tokens y segundos'."""
    agent.budget.max_tokens = 150
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    for _ in range(10):
        fake_model.reply_tool("file.read", {"path": "x.txt"})

    r = agent.chat("trabaja sin parar", "s1")
    assert "detuve" in r.text.lower() or "presupuesto" in r.text.lower()


# ===========================================================================
# PROMESA 6: portabilidad y cero dependencias
# ===========================================================================

def test_promesa_cero_dependencias_en_runtime():
    """README: 'cero dependencias externas — solo stdlib'."""
    import pathlib
    import tomllib

    d = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    assert d["project"]["dependencies"] == []


def test_promesa_el_nucleo_importa_sin_extras():
    """Si un módulo del núcleo necesitara `cryptography`, la instalación en
    Termux se rompería."""
    import importlib

    for mod in ("agent", "journal", "memory", "tools", "identity", "security",
                "store", "tasks", "scheduler", "sync", "forge", "primitives",
                "api", "control", "platform", "subagents"):
        importlib.import_module(f"fibonacci.{mod}")


def test_promesa_cifrado_degrada_declarandolo():
    """SECURITY: 'no llamamos cifrado a las dos cosas por igual'."""
    from fibonacci.crypto import decrypt, encrypt

    blob = encrypt("x", "k", force_fallback=True)
    d = json.loads(blob)
    assert d["enc"] == "fib-hmac-ctr-2"
    assert "_aviso" in d
    assert decrypt(blob, "k") == "x"


def test_promesa_la_plataforma_se_detecta():
    from fibonacci.platform import config_dir, data_dir, detect

    p = detect()
    assert p.os in ("linux", "macos", "windows", "android", "bsd")
    assert data_dir().exists() and config_dir().exists()


# ===========================================================================
# PROMESA 7: trabajo durable
# ===========================================================================

def test_promesa_la_tarea_sobrevive_al_proceso(tmp_path):
    """README: 'el trabajo es un objeto persistido, no un hilo en memoria'."""
    from fibonacci.contracts import DurableTask, Step, TaskState
    from fibonacci.tasks import TaskStore

    store = TaskStore(tmp_path / "t.db")
    t = DurableTask(goal="migrar", session_id="s1",
                    steps=[Step(description=f"paso {i}") for i in range(3)])
    t.cursor = 1
    t.state = TaskState.RUNNING
    store.save(t)

    otro = TaskStore(tmp_path / "t.db")          # como si fuera otro proceso
    recuperada = otro.get(t.id)
    assert recuperada.cursor == 1
    assert recuperada.id in {x.id for x in otro.resumable()}


def test_promesa_el_sync_mueve_el_estado(tmp_path):
    """README: 'ahora sí reanuda desde el teléfono es verdad'."""
    from fibonacci.journal import Journal
    from fibonacci.memory import Memory
    from fibonacci.sync import Sync
    from fibonacci.tasks import TaskStore

    def stack(sub, dev):
        d = tmp_path / sub
        d.mkdir(exist_ok=True)
        s = Sync(Memory(d / "m.db"), Journal(d / "j.db", snapshots=d / "s"),
                 TaskStore(d / "t.db"))
        s.device = dev
        return s

    a, b = stack("a", "laptop"), stack("b", "telefono")
    a.memory.remember(Note("dato de la laptop", kind="fact"))
    bundle = tmp_path / "e.sync"
    a.export(bundle, passphrase="k")
    r = b.import_bundle(bundle, passphrase="k")

    assert r["notas_nuevas"] == 1
    assert any("laptop" in n.content for n in b.memory.recall_all())


# ===========================================================================
# PROMESA 8: subagentes y forja
# ===========================================================================

def test_promesa_los_subagentes_comparten_journal(agent):
    """README: 'undo --all revierte el árbol completo de trabajo'."""
    from fibonacci.subagents import Swarm

    hijo = Swarm(agent)._spawn("s1/sub-1", 0.5)
    assert hijo.journal is agent.journal


def test_promesa_el_presupuesto_se_reparte_entre_subagentes(agent):
    """Sin esto, cinco subagentes gastan cinco veces tu techo."""
    from fibonacci.subagents import Swarm

    s = Swarm(agent)
    hijos = [s._spawn(f"s1/sub-{i}", 0.25) for i in range(4)]
    assert sum(h.budget.max_usd for h in hijos) <= agent.budget.max_usd + 1e-9


def test_promesa_la_forja_rechaza_codigo_peligroso(tmp_path):
    """README: 'análisis estático rechaza eval, ctypes, pickle'."""
    from fibonacci.forge import analyze

    for peligroso in ("import ctypes\ndef run(): pass",
                      "def run(x):\n eval(x)",
                      "import pickle\ndef run(): pass"):
        assert not analyze(peligroso).safe


def test_promesa_el_servidor_mcp_generado_funciona(tmp_path):
    """README: 'genera el protocolo que otros agentes consumen'."""
    import ast
    import subprocess
    import sys

    import fibonacci.forge as F
    from fibonacci.forge import Forge, ForgedTool

    F.data_dir = lambda: tmp_path
    forge = Forge(None)
    t = ForgedTool(name="mayus", description="mayúsculas",
                   code="def run(texto):\n    return texto.upper()",
                   parameters={"type": "object",
                               "properties": {"texto": {"type": "string"}},
                               "required": ["texto"]}, status="tested")
    server = forge.build_mcp_server("acc", [t])
    ast.parse(server.read_text(encoding="utf-8"))

    proc = subprocess.Popen([sys.executable, str(server)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        def rpc(o):
            proc.stdin.write(json.dumps(o) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})[
            "result"]["serverInfo"]["name"] == "acc"
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "mayus", "arguments": {"texto": "hola"}}})
        assert r["result"]["content"][0]["text"] == "HOLA"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


# ===========================================================================
# PROMESA 9: el CLI arranca aunque no haya nada configurado
# ===========================================================================

def test_promesa_doctor_reporta_no_explota(capsys):
    """Sin proveedores, sin config, sin nada: `fib doctor` informa."""
    from fibonacci.cli import main

    codigo = main(["doctor"])
    assert "Fibonacci" in capsys.readouterr().out
    assert codigo in (0, 1)


def test_promesa_el_cli_distingue_subcomando_de_mensaje(capsys):
    """Regresión: el positional `nargs='*'` se comía el subcomando."""
    from fibonacci.cli import main

    assert main(["scope", "list"]) == 0
    assert "Ámbitos" in capsys.readouterr().out


def test_promesa_instalar_expone_el_comando_fib():
    import pathlib
    import tomllib

    d = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "fib" in d["project"]["scripts"]
