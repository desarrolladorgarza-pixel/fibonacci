"""
El modelo como adversario.

Todo lo demás en esta suite prueba a Fibonacci contra endpoints que hablan el
protocolo **correctamente**. Un LLM de verdad no lo hace: envuelve el JSON en
prosa, lo mete en un bloque de código, inventa nombres de herramienta, manda
los argumentos como cadena en vez de objeto, pone un número donde va un texto,
devuelve `content: null`, se corta a media respuesta o simplemente se niega.

Estas pruebas no sustituyen a un modelo vivo — el README sigue diciendo que
eso no se ha hecho — pero cubren **la clase de fallo que un modelo vivo
provoca**, que es lo que de verdad tumba a un agente en producción. La regla
es una sola:

    **Ninguna salida del modelo, por mala que sea, debe propagar una
    excepción fuera de `agent.chat()`.**

El modelo es entrada no confiable. Un agente que revienta con un JSON torcido
no es un agente, es una demo.
"""

from __future__ import annotations

import json

import pytest


# ===========================================================================
# JSON que no viene limpio
# ===========================================================================

@pytest.mark.parametrize("crudo,esperado", [
    # El caso más común de todos: el modelo saluda antes del JSON.
    ('Claro, aquí tienes:\n{"steps": ["uno", "dos"]}', ["uno", "dos"]),
    # Bloque de código markdown, casi tan común como el anterior.
    ('```json\n{"steps": ["uno", "dos"]}\n```', ["uno", "dos"]),
    ('```\n{"steps": ["uno", "dos"]}\n```', ["uno", "dos"]),
    # Prosa por delante y por detrás.
    ('Pensé el plan.\n{"steps": ["uno", "dos"]}\n¿Te parece bien?',
     ["uno", "dos"]),
])
def test_el_plan_sobrevive_al_json_envuelto(agent, fake_model, crudo, esperado):
    """`fib do` depende de esto: si el plan no se parsea, no hay tarea."""
    fake_model.reply(crudo)
    tarea = agent.plan("migrar el blog", "s1")
    assert [p.description for p in tarea.steps] == esperado


def test_un_plan_ilegible_degrada_al_objetivo_en_vez_de_romperse(agent, fake_model):
    """Si no hay JSON rescatable, la tarea es el objetivo tal cual — un paso,
    pero algo que el usuario puede reanudar. Nunca una excepción."""
    fake_model.reply("Lo siento, no puedo ayudarte con eso.")
    tarea = agent.plan("haz algo", "s1")
    assert len(tarea.steps) == 1
    assert tarea.steps[0].description == "haz algo"


def test_json_roto_no_tumba_la_extraccion_de_memoria(agent, fake_model, workspace):
    """La fase de aprendizaje corre después de responder. Si revienta ahí, se
    lleva por delante un turno que ya había tenido éxito."""
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    fake_model.reply_tool("file.read", {"path": "x.txt"})
    fake_model.reply_tool("file.list", {})
    fake_model.reply("listo")
    fake_model.reply('{"notes": [{"content": "sin cerrar"')      # JSON truncado

    r = agent.chat("revisa el proyecto y dime qué encuentras", "s1")
    assert "listo" in r.text
    assert len(r.tools_used) == 2


# ===========================================================================
# Tool calls que un modelo real produce y un doble educado no
# ===========================================================================

def test_una_herramienta_inventada_se_reporta_y_el_turno_sigue(agent, fake_model):
    """
    Los modelos pequeños alucinan nombres de herramienta constantemente.

    El turno no debe morir: el error vuelve al modelo como resultado, que es
    justo la información que necesita para corregir.
    """
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.teletransporta",
                                     "arguments": "{}"}}]})
    fake_model.reply("perdón, me equivoqué de herramienta")

    r = agent.chat("haz algo raro", "s1")
    assert "equivoqué" in r.text
    tool_msgs = [m for m in fake_model.requests[-1]["body"]["messages"]
                 if m.get("role") == "tool"]
    assert tool_msgs and "desconocida" in tool_msgs[0]["content"].lower()


def test_argumentos_que_no_son_json_no_tumban_el_turno(agent, fake_model):
    """`arguments` es una cadena JSON; un modelo puede mandar cualquier cosa."""
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.list",
                                     "arguments": "esto no es json"}}]})
    fake_model.reply("ya está")

    r = agent.chat("lista los archivos", "s1")
    assert r.text == "ya está"


def test_argumentos_como_cadena_en_vez_de_objeto(agent, fake_model, workspace):
    """
    `"arguments": "\\"notas.txt\\""` produce una cadena, no un dict.

    Es un fallo real de modelos locales pequeños, y todo el camino de
    invocación asume un dict: sin defensa, revienta antes incluso de intentar
    ejecutar la herramienta.
    """
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.write",
                                     "arguments": json.dumps("notas.txt")}}]})
    fake_model.reply("hecho")

    r = agent.chat("escribe notas", "s1")
    assert r.text == "hecho"


def test_argumentos_del_tipo_equivocado(agent, fake_model):
    """Un entero donde va una ruta. La herramienta falla; el agente no."""
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.read",
                                     "arguments": '{"path": 12345}'}}]})
    fake_model.reply("no pude leerlo")

    r = agent.chat("lee algo", "s1")
    assert r.text == "no pude leerlo"


def test_argumentos_de_mas_no_revientan(agent, fake_model, workspace):
    """El modelo añade un parámetro que la herramienta no acepta."""
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.write",
                                     "arguments": json.dumps(
                                         {"path": "a.txt", "content": "x",
                                          "encoding": "utf-8", "modo": 777})}}]})
    fake_model.reply("listo")

    r = agent.chat("escribe a.txt", "s1")
    assert r.text == "listo"


def test_falta_un_argumento_obligatorio(agent, fake_model):
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "file.write",
                                     "arguments": '{"path": "a.txt"}'}}]})
    fake_model.reply("me faltó el contenido")

    r = agent.chat("escribe a.txt", "s1")
    assert r.text == "me faltó el contenido"


def test_varias_herramientas_en_un_solo_mensaje(agent, fake_model, workspace):
    """Los modelos con tool calling paralelo mandan varias de golpe."""
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    (workspace / "b.txt").write_text("B", encoding="utf-8")
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "file.read", "arguments": '{"path": "a.txt"}'}},
            {"id": "c2", "type": "function",
             "function": {"name": "file.read", "arguments": '{"path": "b.txt"}'}}]})
    fake_model.reply("leí las dos")

    r = agent.chat("lee a.txt y b.txt", "s1")
    assert r.tools_used == ["file.read", "file.read"]
    assert r.text == "leí las dos"


# ===========================================================================
# Respuestas degeneradas
# ===========================================================================

def test_content_nulo(agent, fake_model):
    """`content: null` es legal en el protocolo y rompe cualquier `.strip()`."""
    fake_model.reply_raw({"role": "assistant", "content": None})
    r = agent.chat("hola", "s1")
    assert r.text == ""


def test_respuesta_cortada_por_longitud(agent, fake_model):
    """`finish_reason: length` — el modelo se quedó sin espacio a mitad."""
    fake_model.reply_raw({"role": "assistant",
                          "content": "Estaba explicando algo cuando me qued"},
                         finish="length")
    r = agent.chat("explícame algo largo", "s1")
    assert r.text.startswith("Estaba explicando")


def test_mensaje_sin_role_ni_content(agent, fake_model):
    fake_model.reply_raw({})
    r = agent.chat("hola", "s1")
    assert r.text == ""


def test_tool_call_sin_nombre_se_descarta(agent, fake_model):
    """
    Una llamada sin nombre es inservible: no hay herramienta que invocar ni
    forma de adivinarla, así que se descarta y el turno termina con lo que el
    modelo haya dicho. Lo que importa es que no se invente una herramienta ni
    reviente por el camino.
    """
    fake_model.reply_raw({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"arguments": "{}"}}]})

    r = agent.chat("haz algo", "s1")
    assert r.text == ""
    assert not r.tools_used, "no debe ejecutarse nada"


def test_el_modelo_se_niega(agent, fake_model):
    """Una negativa es una respuesta válida, no un error."""
    fake_model.reply("No puedo ayudarte con eso.")
    r = agent.chat("haz algo que no quiero hacer", "s1")
    assert "No puedo" in r.text
    assert not r.tools_used


def test_bucle_de_herramienta_repetida_termina(agent, fake_model, workspace):
    """
    Un modelo atascado repite la misma llamada para siempre. El tope existe
    justo para esto, y debe cortar sin excepción y avisando.
    """
    (workspace / "x.txt").write_text("x", encoding="utf-8")
    for _ in range(40):
        fake_model.reply_tool("file.read", {"path": "x.txt"})

    from fibonacci.agent import MAX_TOOL_STEPS

    r = agent.chat("lee en bucle", "s1")
    assert len(r.tools_used) <= MAX_TOOL_STEPS
    assert r.truncated


# ===========================================================================
# La regla, de una vez y sobre todo lo anterior
# ===========================================================================

MALFORMACIONES = [
    ("content nulo", {"role": "assistant", "content": None}),
    ("mensaje vacío", {}),
    ("tool_calls no es lista",
     {"role": "assistant", "content": "", "tool_calls": {}}),
    ("tool_call sin function",
     {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}),
    ("function no es objeto",
     {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "function": "file.read"}]}),
    ("arguments nulo",
     {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "function": {"name": "file.list", "arguments": None}}]}),
    ("arguments es una lista",
     {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "function": {"name": "file.list",
                                   "arguments": "[1, 2, 3]"}}]}),
    ("arguments es un número",
     {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "function": {"name": "file.list", "arguments": "42"}}]}),
    ("nombre con ruta rara",
     {"role": "assistant", "content": "", "tool_calls": [
         {"id": "c1", "function": {"name": "../../etc/passwd",
                                   "arguments": "{}"}}]}),
    ("content es un número", {"role": "assistant", "content": 42}),
]


@pytest.mark.parametrize("etiqueta,mensaje",
                         MALFORMACIONES, ids=[m[0] for m in MALFORMACIONES])
def test_ninguna_salida_del_modelo_propaga_una_excepcion(agent, fake_model,
                                                         etiqueta, mensaje):
    """
    La invariante del archivo: el modelo es entrada no confiable.

    Da igual lo torcido que venga — el turno puede fallar, puede no hacer
    nada, puede devolver texto vacío; lo que no puede es lanzar hacia fuera y
    tumbar el CLI, la superficie de Telegram o el servidor MCP.
    """
    fake_model.reply_raw(mensaje)
    fake_model.default("de acuerdo")

    r = agent.chat("haz lo que puedas", "s1")
    assert isinstance(r.text, str)
