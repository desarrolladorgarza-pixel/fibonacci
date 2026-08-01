"""
Pruebas del bucle completo del agente contra el modelo falso.

**Este archivo es la plantilla.** Hasta la v0.6.0 nada de esto se podía probar
porque hacía falta un LLM vivo; con `fake_model` (ver `conftest.py`) el camino
entero es ejercitable sin red ni GPU.

Está deliberadamente incompleto: cubre los casos que demuestran *cómo* se
prueba cada cosa, y deja marcados con `# TAREA:` los huecos que faltan. Ver
`CODEX.md` para la lista completa y el criterio de aceptación.
"""

from __future__ import annotations


import pytest

from fibonacci.contracts import Capability, Message, Note, Skill, ToolSpec
from fibonacci.mesh.providers import ProviderError


# ===========================================================================
# Mesh: la capa que nunca se había probado
# ===========================================================================

def test_ask_devuelve_texto_y_contabiliza(mesh, fake_model):
    fake_model.reply("hola")
    c = mesh.ask(Capability.CHAT, [Message("user", "hola")])
    assert c.text == "hola"
    assert c.prompt_tokens == 100
    assert mesh.ledger.calls == 1


def test_tool_calls_se_parsean(mesh, fake_model):
    fake_model.reply_tool("file.read", {"path": "x.txt"})
    c = mesh.ask(Capability.CHAT, [Message("user", "lee")],
                 tools=[ToolSpec("file.read", "lee",
                                 {"type": "object", "properties": {}})])
    assert len(c.tool_calls) == 1
    assert c.tool_calls[0].name == "file.read"
    assert c.tool_calls[0].arguments == {"path": "x.txt"}


def test_las_herramientas_llegan_al_modelo(mesh, fake_model):
    fake_model.reply("ok")
    mesh.ask(Capability.CHAT, [Message("user", "x")],
             tools=[ToolSpec("a", "d", {"type": "object", "properties": {}}),
                    ToolSpec("b", "d", {"type": "object", "properties": {}})])
    assert set(fake_model.last_tools()) == {"a", "b"}


def test_circuit_breaker_abre_tras_fallos(mesh, fake_model):
    fake_model.fail(500, times=5)
    for _ in range(3):
        with pytest.raises(ProviderError):
            mesh.ask(Capability.CHAT, [Message("user", "x")])
    assert mesh.breaker.is_open("qwen3:8b")


def test_streaming_emite_fragmentos(mesh, fake_model):
    fake_model.reply("uno dos tres")
    frags = list(mesh.stream(Capability.CHAT, [Message("user", "x")]))
    assert len(frags) >= 3
    assert "".join(frags).strip() == "uno dos tres"


def test_embeddings_son_deterministas(mesh):
    assert mesh.embed(["hola"]) == mesh.embed(["hola"])
    assert mesh.embed(["hola"]) != mesh.embed(["adiós"])


def test_modo_local_no_sale_a_la_nube(mesh):
    """En modo local, si no hay modelo local para la capacidad, FALLA."""
    from fibonacci.contracts import Capability as C

    candidatos = mesh.catalog.find(C.CHAT, local_only=True)
    assert candidatos and all(c.local for c in candidatos)


# TAREA: cascada de respaldo — dos proveedores, el primero falla, el segundo
# responde. Requiere un mesh con dos providers apuntando a dos fake_model.
# TAREA: modo hybrid ordena locales antes que nube aunque la nube tenga mejor
# prioridad.
# TAREA: min_context filtra modelos con ventana insuficiente.


# ===========================================================================
# Bucle del agente
# ===========================================================================

def test_conversacion_simple(agent, fake_model):
    fake_model.reply("Son las tres").reply_json({"notes": [], "skill": None})
    r = agent.chat("¿qué hora es?", "s1")
    assert r.text == "Son las tres"
    assert not r.tools_used


def test_bucle_de_herramientas_completo(agent, fake_model, workspace):
    """El agente pide una herramienta, recibe el resultado y responde."""
    (workspace / "notas.txt").write_text("contenido secreto", encoding="utf-8")

    fake_model.reply_tool("file.read", {"path": "notas.txt"})
    fake_model.reply("El archivo dice: contenido secreto")
    fake_model.reply_json({"notes": [], "skill": None})

    r = agent.chat("¿qué dice notas.txt?", "s1")
    assert r.tools_used == ["file.read"]
    assert "contenido secreto" in r.text

    # El resultado de la herramienta debe volver al modelo como rol "tool"
    segunda = fake_model.requests[1]["body"]["messages"]
    tool_msgs = [m for m in segunda if m.get("role") == "tool"]
    assert tool_msgs and "contenido secreto" in tool_msgs[0]["content"]


def test_escritura_por_el_agente_es_reversible(agent, fake_model, workspace):
    fake_model.reply_tool("file.write", {"path": "nuevo.md", "content": "hola"})
    fake_model.reply("Creado")
    fake_model.reply_json({"notes": [], "skill": None})

    r = agent.chat("crea nuevo.md", "s1")
    assert (workspace / "nuevo.md").exists()
    assert len(r.actions) == 1

    ok, _ = agent.journal.undo_last("s1")
    assert ok and not (workspace / "nuevo.md").exists()


def test_tope_de_herramientas_corta_el_bucle(agent, fake_model, workspace):
    """Un modelo que pide herramientas sin parar no debe girar para siempre."""
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    for _ in range(30):
        fake_model.reply_tool("file.read", {"path": "x.txt"})

    r = agent.chat("lee en bucle", "s1")
    from fibonacci.agent import MAX_TOOL_STEPS

    assert len(r.tools_used) <= MAX_TOOL_STEPS
    assert r.truncated


def test_presupuesto_interrumpe_a_media_conversacion(agent, fake_model, workspace):
    agent.budget.max_tokens = 150          # una vuelta y media
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    for _ in range(10):
        fake_model.reply_tool("file.read", {"path": "x.txt"})

    r = agent.chat("trabaja mucho", "s1")
    assert "detuve" in r.text.lower() or "presupuesto" in r.text.lower()


def test_memoria_relevante_entra_al_system(agent, fake_model):
    agent.memory.remember(Note("el proyecto principal es VIGIA", kind="project"))
    fake_model.reply("ok").reply_json({"notes": [], "skill": None})
    agent.chat("cómo va el proyecto", "s1")
    assert "VIGIA" in fake_model.last_system()


def test_skill_activa_entra_al_system(agent, fake_model):
    agent.memory.save_skill(Skill(name="respaldo", body="paso 1: comprimir",
                                  triggers=["respaldo"], status="active",
                                  trials=10, wins=9))
    fake_model.reply("ok").reply_json({"notes": [], "skill": None})
    agent.chat("haz un respaldo", "s1")
    assert "paso 1: comprimir" in fake_model.last_system()


def test_skill_candidata_no_entra(agent, fake_model):
    agent.memory.save_skill(Skill(name="nueva", body="NO DEBE APARECER",
                                  triggers=["respaldo"], status="candidate"))
    fake_model.reply("ok").reply_json({"notes": [], "skill": None})
    agent.chat("haz un respaldo", "s1")
    assert "NO DEBE APARECER" not in fake_model.last_system()


def test_el_turno_anterior_entra_como_historial(agent, fake_model):
    fake_model.reply("primera")
    agent.chat("hola", "s1")
    fake_model.reply("segunda")

    # Marcamos la posición en vez de contar desde el final: cuántas peticiones
    # hace un turno depende de si `_maybe_learn` se dispara, y aquí no lo hace.
    marca = len(fake_model.requests)
    agent.chat("y ahora", "s1")

    roles = [m["role"] for m in fake_model.requests[marca]["body"]["messages"]]
    assert roles.count("user") >= 2, "el historial debe reinyectarse"


def test_aprende_una_nota_tras_interaccion_sustantiva(agent, fake_model, workspace):
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    fake_model.reply_tool("file.read", {"path": "x.txt"})
    fake_model.reply_tool("file.list", {})
    fake_model.reply("listo")
    fake_model.reply_json({"notes": [{"content": "usa Python 3.12",
                                      "kind": "fact", "confidence": 0.8,
                                      "half_life_days": 365}],
                           "skill": None})
    agent.chat("revisa mi proyecto y dime qué versión de Python uso", "s1")
    assert any("3.12" in n.content for n in agent.memory.recall_all())


def test_streaming_del_agente(agent, fake_model):
    fake_model.reply("respuesta en fragmentos")
    frags = list(agent.chat_stream("hola", "s1"))
    assert "".join(frags).strip() == "respuesta en fragmentos"


def test_streaming_cede_cuando_hay_herramientas(agent, fake_model, workspace):
    """Con intención de acción, no se transmite: se usa el camino normal."""
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    fake_model.reply("hecho").reply_json({"notes": [], "skill": None})
    frags = list(agent.chat_stream("crea un archivo nuevo", "s1"))
    assert frags and "hecho" in "".join(frags)


# TAREA: la contaminación se reinicia entre turnos (tools.taint.reset()).
# TAREA: el veredicto de skill llega en el turno siguiente (ya cubierto en
#        test_correcciones con mesh falso; falta la versión con fake_model).
# TAREA: el presupuesto de contexto recorta el historial cuando la ventana es
#        pequeña — usar fake_model.approx_prompt_chars() para afirmarlo.
# TAREA: enrutamiento por intención elige CODE ante "arregla este bug".


# ===========================================================================
# Tareas durables y subagentes
# ===========================================================================

def test_plan_descompone_en_pasos(agent, fake_model):
    fake_model.reply_json({"steps": ["exportar", "convertir", "publicar"]})
    tarea = agent.plan("migrar el blog", "s1")
    assert len(tarea.steps) == 3
    assert tarea.steps[0].description == "exportar"


def test_tarea_avanza_paso_a_paso(agent, fake_model):
    fake_model.reply_json({"steps": ["uno", "dos"]})
    tarea = agent.plan("objetivo", "s1")
    fake_model.default("paso completado")

    estados = [t.cursor for t in agent.advance(tarea)]
    assert estados == [1, 2]
    assert tarea.state.value == "done"


# TAREA: un paso que falla deja la tarea en FAILED y es reanudable.
# TAREA: Swarm.solve() con fake_model: descomponer, paralelizar, sintetizar.


# ===========================================================================
# CLI — 1,011 líneas sin una sola prueba
# ===========================================================================

def test_doctor_no_explota_sin_proveedores(capsys, monkeypatch):
    """`fib doctor` debe reportar, nunca lanzar."""
    from fibonacci.cli import main

    codigo = main(["doctor"])
    salida = capsys.readouterr().out
    assert "Fibonacci" in salida
    assert codigo in (0, 1)


def test_config_lee_y_escribe(capsys):
    from fibonacci.cli import main

    assert main(["config", "mode", "local"]) == 0
    main(["config"])
    assert "local" in capsys.readouterr().out


# TAREA: `fib scope list|add`, `fib pair`, `fib memory list|conflicts`,
#        `fib skills`, `fib history`, `fib undo`, `fib schedule add|list`,
#        `fib vault add|list`, `fib forge list`, `fib tasks`.
#        Con `isolate` autouse ninguna toca el home real.
# TAREA: el despacho de subcomandos vs mensaje directo (`fib "hola"` vs
#        `fib scope list`) — hubo un bug real ahí.
