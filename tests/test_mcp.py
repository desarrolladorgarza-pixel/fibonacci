"""
Servidor MCP.

Es la puerta por la que Claude y cualquier otro host de MCP usan Fibonacci, y
la afirmación más fuerte del README sobre esta pieza es concreta: *«es el único
servidor MCP donde el host puede revertir lo que la herramienta hizo»*.

El host es un proceso ajeno hablando JSON-RPC por stdio. Como cualquier
frontera con el exterior, la regla es que **nada de lo que mande puede tumbar
el servidor**: un método desconocido, parámetros que faltan o una herramienta
inventada tienen que volver como respuesta, no como excepción. Si el servidor
muere, el host pierde la sesión entera.
"""

from __future__ import annotations

import io
import json

import pytest

from fibonacci.contracts import Note


@pytest.fixture
def servidor(agent):
    """Un `MCPServer` con el agente de pruebas ya inyectado (sin `boot()`)."""
    from fibonacci.mcp import MCPServer

    s = MCPServer()
    s._agent = agent
    return s


def _pedir(servidor, method, **params):
    return servidor.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params})


def _texto(resp):
    return resp["result"]["content"][0]["text"]


# ===========================================================================
# Protocolo
# ===========================================================================

def test_initialize_declara_version_y_nombre(servidor):
    from fibonacci import __version__

    r = _pedir(servidor, "initialize")
    assert r["result"]["serverInfo"] == {"name": "fibonacci",
                                         "version": __version__}
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in r["result"]["capabilities"]


def test_tools_list_publica_las_cuatro(servidor):
    r = _pedir(servidor, "tools/list")
    nombres = {t["name"] for t in r["result"]["tools"]}
    assert nombres == {"fibonacci_do", "fibonacci_undo",
                       "fibonacci_history", "fibonacci_recall"}


def test_cada_herramienta_declara_su_esquema(servidor):
    """Un host que no puede leer el esquema no sabe cómo llamarte."""
    for t in _pedir(servidor, "tools/list")["result"]["tools"]:
        assert t["description"], t["name"]
        assert t["inputSchema"]["type"] == "object"


def test_las_notificaciones_no_se_responden(servidor):
    """JSON-RPC: una notificación no lleva respuesta. Contestarla confunde
    al host."""
    assert servidor.handle({"jsonrpc": "2.0",
                            "method": "notifications/initialized"}) is None


def test_un_metodo_desconocido_devuelve_error_no_excepcion(servidor):
    r = servidor.handle({"jsonrpc": "2.0", "id": 7, "method": "cosas/raras"})
    assert r["error"]["code"] == -32601
    assert r["id"] == 7


# ===========================================================================
# Las herramientas
# ===========================================================================

def test_do_ejecuta_y_anuncia_que_es_reversible(servidor, fake_model, workspace):
    """El encabezado es lo que le dice al host que puede deshacer."""
    fake_model.reply_tool("file.write", {"path": "nota.md", "content": "hola"})
    fake_model.reply("listo")

    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_do",
                      arguments={"instruction": "escribe nota.md",
                                 "session": "m1"}))
    assert "file.write" in t
    assert "reversibles con fibonacci_undo" in t
    assert (workspace / "nota.md").exists()


def test_el_host_revierte_lo_que_la_herramienta_hizo(servidor, fake_model,
                                                     workspace):
    """La afirmación distintiva del README, de punta a punta."""
    fake_model.reply_tool("file.write", {"path": "nota.md", "content": "hola"})
    fake_model.reply("listo")
    _pedir(servidor, "tools/call", name="fibonacci_do",
           arguments={"instruction": "escribe nota.md", "session": "m1"})
    assert (workspace / "nota.md").exists()

    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_undo",
                      arguments={"session": "m1"}))
    assert "nota.md" in t
    assert not (workspace / "nota.md").exists()


def test_undo_all_revierte_la_sesion(servidor, fake_model, workspace):
    for nombre in ("a.txt", "b.txt"):
        fake_model.reply_tool("file.write", {"path": nombre, "content": "x"})
        fake_model.reply("hecho")
        _pedir(servidor, "tools/call", name="fibonacci_do",
               arguments={"instruction": f"escribe {nombre}", "session": "m2"})

    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_undo",
                      arguments={"session": "m2", "all": True}))
    assert t.startswith("2 revertidas")
    assert not (workspace / "a.txt").exists()
    assert not (workspace / "b.txt").exists()


def test_undo_sin_nada_que_deshacer_lo_dice(servidor):
    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_undo",
                      arguments={"session": "vacia"}))
    assert "nada que deshacer" in t.lower()


def test_history_vacio_y_con_datos(servidor, fake_model, workspace):
    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_history",
                      arguments={}))
    assert "Sin cambios" in t

    fake_model.reply_tool("file.write", {"path": "x.md", "content": "y"})
    fake_model.reply("ok")
    _pedir(servidor, "tools/call", name="fibonacci_do",
           arguments={"instruction": "escribe x.md", "session": "m3"})

    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_history",
                      arguments={"session": "m3"}))
    assert "file.write" in t and "applied" in t


def test_recall_devuelve_lo_que_sabe_con_su_confianza(servidor, agent):
    agent.memory.remember(Note("el proyecto principal es VIGIA", kind="project"))

    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_recall",
                      arguments={"query": "proyecto"}))
    assert "VIGIA" in t and "%" in t, "la confianza decaída debe ser visible"


def test_recall_sin_datos_lo_dice(servidor):
    t = _texto(_pedir(servidor, "tools/call", name="fibonacci_recall",
                      arguments={"query": "algo que no sabe"}))
    assert "Sin datos" in t


# ===========================================================================
# El host es entrada no confiable
# ===========================================================================

@pytest.mark.parametrize("params", [
    {"name": "fibonacci_inventada", "arguments": {}},
    {"name": "", "arguments": {}},
    {"name": "fibonacci_do", "arguments": {}},          # falta `instruction`
    {"name": "fibonacci_recall", "arguments": {}},      # falta `query`
    {"name": "fibonacci_history", "arguments": {"limit": "muchos"}},
])
def test_una_llamada_mal_formada_vuelve_como_error(servidor, params):
    """
    Cualquier fallo se devuelve con `isError`, nunca como excepción: si el
    servidor muere, el host pierde la sesión entera.
    """
    r = servidor.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": params})
    assert r["result"]["isError"] is True
    assert r["result"]["content"][0]["text"].startswith("Error:")


def test_tools_call_sin_params(servidor):
    r = servidor.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert r["result"]["isError"] is True


# ===========================================================================
# El bucle de stdio
# ===========================================================================

def test_el_bucle_ignora_lineas_vacias_y_json_roto(monkeypatch, agent):
    """
    Un host puede mandar una línea en blanco o cortarse a media escritura. El
    servidor debe seguir sirviendo, no morirse en la línea siguiente.
    """
    import fibonacci.mcp as mcp

    entrada = io.StringIO(
        "\n"
        "{esto no es json\n"
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        "\n"
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    salida = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdin", entrada)
    monkeypatch.setattr(mcp.sys, "stdout", salida)
    monkeypatch.setattr(mcp, "boot", lambda **kw: agent)

    mcp.main()

    lineas = [x for x in salida.getvalue().splitlines() if x.strip()]
    respuestas = [json.loads(x) for x in lineas]
    assert [r["id"] for r in respuestas] == [1, 2], \
        "las dos peticiones válidas se responden; la basura se ignora"
