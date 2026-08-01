"""
Pruebas del CLI.

`cli.py` era el módulo más grande del proyecto y el único sin una sola prueba:
un quinto del código, y la puerta por la que pasa todo usuario. Ver `CODEX.md`
§3 (P0).

Se invoca `main([...])` directamente. Gracias a `isolate` (autouse) ninguna de
estas pruebas toca el home real: el `config.json`, el journal, la memoria, la
bóveda y las tareas programadas viven en un tmp por prueba.

**Nada aquí habla con un modelo.** Los comandos que necesitan uno reciben un
agente falso por `monkeypatch` de `cli._boot`; los demás corren contra el
código real. Ningún caso sale a la red.
"""

from __future__ import annotations

import json
import time

import pytest

from fibonacci.contracts import Action, Note, Skill


@pytest.fixture
def cli(isolate):
    """El módulo, ya aislado. Importarlo aquí garantiza que `config_dir` esté
    parcheado antes de que nada resuelva una ruta."""
    import fibonacci.cli as m

    return m


def _salida(capsys) -> str:
    return capsys.readouterr().out


# ===========================================================================
# Despacho: `fib "hola"` contra `fib scope list`
# ===========================================================================

def test_un_mensaje_directo_no_se_confunde_con_un_subcomando(cli, monkeypatch):
    """El bug real: el positional `nargs='*'` se comía el nombre del
    subcomando, así que `fib scope list` se interpretaba como conversación."""
    vistos = []
    monkeypatch.setattr(cli, "cmd_chat", lambda a: vistos.append(a.message) or 0)

    assert cli.main(["hola", "qué", "tal"]) == 0
    assert vistos == [["hola", "qué", "tal"]]


def test_un_subcomando_no_se_confunde_con_un_mensaje(cli, capsys):
    assert cli.main(["scope", "list"]) == 0
    assert "Ámbitos" in _salida(capsys)


def test_sin_argumentos_el_mensaje_queda_vacio(cli, monkeypatch):
    vistos = []
    monkeypatch.setattr(cli, "cmd_chat", lambda a: vistos.append(a.message) or 0)
    assert cli.main([]) == 0
    assert vistos == [[]]


# ===========================================================================
# config
# ===========================================================================

def test_config_escribe_lee_y_lista(cli, capsys):
    assert cli.main(["config", "mode", "local"]) == 0
    capsys.readouterr()

    assert cli.main(["config", "mode"]) == 0
    assert _salida(capsys).strip() == "local"

    assert cli.main(["config"]) == 0
    assert "profile = hybrid" in _salida(capsys)


def test_config_no_escribe_fuera_del_directorio_aislado(cli, isolate):
    """La configuración se resuelve al usarla, no al importar el módulo."""
    cli.main(["config", "session", "trabajo"])
    destino = isolate["config"] / "config.json"
    assert destino.exists()
    assert json.loads(destino.read_text(encoding="utf-8"))["session"] == "trabajo"


def test_config_corrupto_no_tumba_el_cli(cli, isolate, capsys):
    (isolate["config"] / "config.json").write_text("{esto no es json", encoding="utf-8")
    assert cli.main(["config"]) == 0
    assert "profile = hybrid" in _salida(capsys)


# ===========================================================================
# scope — el control de autonomía real
# ===========================================================================

def test_scope_add_registra_el_ambito(cli, capsys):
    assert cli.main(["scope", "add", "~/proyectos/**", "libre",
                     "--note", "mi código"]) == 0
    assert "✓" in _salida(capsys) or "proyectos" in _salida(capsys)

    assert cli.main(["scope", "list"]) == 0
    assert "proyectos" in _salida(capsys)


def test_scope_no_puede_anular_un_bloqueo_del_nucleo(cli):
    """Las reglas CORE_DENY existen para no poder desactivarse desde el CLI:
    si un `scope add` pudiera abrirlas, el resto de la defensa sobra."""
    from fibonacci.identity import Authority, Decision

    a = Authority.load()
    nucleo = [s for s in a.scopes if s.decision is Decision.DENY]
    assert nucleo, "debe haber bloqueos de núcleo"
    patron = nucleo[0].pattern

    cli.main(["scope", "add", patron, "libre"])

    de_nuevo = Authority.load()
    decision, _ = de_nuevo.check(de_nuevo.principal("cli:local"),
                                 "shell.run", patron.replace("**", "x"))
    assert decision is Decision.DENY


def test_scope_add_sobre_un_bloqueo_de_nucleo_lo_advierte(cli, capsys):
    """El comportamiento ya era correcto; la salida engañaba. Decía
    '✓ /etc/** → libre' y el usuario se quedaba creyendo que había abierto
    /etc, cuando el bloqueo de núcleo seguía ganando."""
    assert cli.main(["scope", "add", "/etc/**", "libre"]) == 0
    salida = _salida(capsys)
    assert "BLOQUEADO" in salida
    assert "✓ /etc/** → libre" not in salida


def test_scope_add_normal_sigue_confirmando(cli, capsys):
    assert cli.main(["scope", "add", "~/proyectos/mio/**", "libre"]) == 0
    assert "✓" in _salida(capsys)


# ===========================================================================
# pair
# ===========================================================================

def test_pair_emite_un_codigo(cli, capsys):
    assert cli.main(["pair"]) == 0
    assert "código" in _salida(capsys)


def test_pair_list_muestra_los_emparejados(cli, capsys):
    from fibonacci.identity import Authority, Principal, Trust

    a = Authority.load()
    a.principals["telegram:9"] = Principal("telegram:9", "Ana", Trust.MEMBER)
    a.save()

    assert cli.main(["pair", "--list"]) == 0
    salida = _salida(capsys)
    assert "telegram:9" in salida and "Ana" in salida


def test_pair_revoke_borra_una_sola_vez(cli, capsys):
    """`revoke` muta. Se llamaba dos veces —una para el texto y otra para el
    color— y el éxito salía pintado como fallo."""
    from fibonacci.identity import Authority, Principal, Trust

    a = Authority.load()
    a.principals["telegram:9"] = Principal("telegram:9", "Ana", Trust.MEMBER)
    a.save()

    assert cli.main(["pair", "--revoke", "telegram:9"]) == 0
    assert "revocado" in _salida(capsys)
    assert "telegram:9" not in Authority.load().principals


def test_pair_revoke_de_un_desconocido_lo_dice(cli, capsys):
    assert cli.main(["pair", "--revoke", "telegram:nadie"]) == 0
    assert "no existe" in _salida(capsys)


# ===========================================================================
# memory
# ===========================================================================

def test_memory_list_ordena_por_confianza(cli, capsys):
    from fibonacci.memory import Memory

    m = Memory()
    m.remember(Note("vive en Monterrey", kind="fact", confidence=0.9))
    m.remember(Note("prefiere el café sin azúcar", kind="preference",
                    confidence=0.4))

    assert cli.main(["memory", "list"]) == 0
    salida = _salida(capsys)
    assert salida.index("Monterrey") < salida.index("café")


def test_memory_search_pone_primero_lo_que_coincide(cli, capsys):
    """`recall` ordena; no filtra. La nota que coincide con la consulta debe
    quedar por encima de la que no."""
    from fibonacci.memory import Memory

    m = Memory()
    m.remember(Note("su gato se llama Newton", kind="fact"))
    m.remember(Note("el proyecto principal es VIGIA", kind="project"))

    assert cli.main(["memory", "search", "proyecto"]) == 0
    salida = _salida(capsys)
    assert salida.index("VIGIA") < salida.index("Newton")


def test_memory_conflicts_sin_conflictos_lo_dice(cli, capsys):
    assert cli.main(["memory", "conflicts"]) == 0
    assert "Sin contradicciones" in _salida(capsys)


def test_memory_conflicts_muestra_las_dos_versiones(cli, capsys):
    from fibonacci.memory import Memory

    m = Memory()
    m.remember(Note("trabaja en Acme como diseñador", kind="fact"))
    m.remember(Note("trabaja en Acme como programador", kind="fact"))

    assert cli.main(["memory", "conflicts"]) == 0
    salida = _salida(capsys)
    assert "diseñador" in salida and "programador" in salida


def test_memory_prune_retira_lo_caducado(cli, capsys):
    assert cli.main(["memory", "prune"]) == 0
    assert "caducadas" in _salida(capsys)


def test_memory_sin_accion_muestra_estadisticas(cli, capsys):
    assert cli.main(["memory"]) == 0
    assert _salida(capsys).strip()


# ===========================================================================
# skills
# ===========================================================================

def test_skills_vacio_explica_como_nacen(cli, capsys):
    assert cli.main(["skills"]) == 0
    assert "procedimientos" in _salida(capsys)


def test_skills_ordena_activas_antes_que_candidatas(cli, capsys):
    from fibonacci.memory import Memory

    m = Memory()
    m.save_skill(Skill(name="candidata-nueva", body="x", status="candidate"))
    m.save_skill(Skill(name="activa-vieja", body="y", status="active",
                       trials=10, wins=9))

    assert cli.main(["skills"]) == 0
    salida = _salida(capsys)
    assert salida.index("activa-vieja") < salida.index("candidata-nueva")


# ===========================================================================
# history / undo
# ===========================================================================

def _accion(session: str = "principal", tool: str = "file.write"):
    """Registra una acción real en el journal aislado y devuelve su id."""
    from fibonacci.journal import Journal

    j = Journal()
    act = Action(tool=tool, arguments={"path": "x.txt"}, session_id=session)
    j.record(act)
    return j, act


def test_history_vacio_lo_dice(cli, capsys):
    assert cli.main(["history"]) == 0
    assert "no ha modificado nada" in _salida(capsys)


def test_history_lista_las_acciones_y_sus_estadisticas(cli, capsys):
    _accion()
    assert cli.main(["history"]) == 0
    salida = _salida(capsys)
    assert "file.write" in salida
    assert "acciones" in salida


def test_history_trace_muestra_el_razonamiento(cli, capsys):
    from fibonacci.journal import Journal

    Journal().trace("principal", "turno", "capacidad=chat ventana=32768")

    assert cli.main(["history", "--trace"]) == 0
    salida = _salida(capsys)
    assert "turno" in salida and "capacidad=chat" in salida


def test_undo_sin_nada_que_deshacer_devuelve_1(cli, capsys):
    assert cli.main(["undo"]) == 1
    assert "nada que deshacer" in _salida(capsys).lower()


def test_undo_revierte_la_ultima_escritura(cli, capsys, isolate):
    """El comando que ningún otro agente tiene."""
    from fibonacci.journal import Journal
    from fibonacci.tools import ToolBox

    ws = isolate["workspace"]
    (ws / "doc.md").write_text("v1", encoding="utf-8")

    j = Journal()
    box = ToolBox(j, root=ws, confirm=lambda d, x: True)
    box.invoke("file.write", {"path": "doc.md", "content": "v2"}, "principal")
    assert (ws / "doc.md").read_text(encoding="utf-8") == "v2"

    assert cli.main(["undo"]) == 0
    assert (ws / "doc.md").read_text(encoding="utf-8") == "v1"


def test_undo_all_revierte_la_sesion_completa(cli, capsys, isolate):
    from fibonacci.journal import Journal
    from fibonacci.tools import ToolBox

    ws = isolate["workspace"]
    j = Journal()
    box = ToolBox(j, root=ws, confirm=lambda d, x: True)
    for nombre in ("a.txt", "b.txt"):
        box.invoke("file.write", {"path": nombre, "content": "x"}, "principal")

    assert cli.main(["undo", "--all"]) == 0
    assert "2 accion(es) revertidas" in _salida(capsys)
    assert not (ws / "a.txt").exists() and not (ws / "b.txt").exists()


def test_undo_se_niega_si_el_archivo_cambio_despues(cli, capsys, isolate):
    """La garantía central: un undo que borra trabajo nuevo en silencio sería
    peor que no tener undo."""
    from fibonacci.journal import Journal
    from fibonacci.tools import ToolBox

    ws = isolate["workspace"]
    j = Journal()
    box = ToolBox(j, root=ws, confirm=lambda d, x: True)
    box.invoke("file.write", {"path": "doc.md", "content": "del agente"},
               "principal")
    (ws / "doc.md").write_text("editado a mano", encoding="utf-8")

    assert cli.main(["undo"]) == 1
    assert (ws / "doc.md").read_text(encoding="utf-8") == "editado a mano"
    assert "--force" in _salida(capsys)


def test_undo_force_procede_pese_al_cambio(cli, isolate):
    from fibonacci.journal import Journal
    from fibonacci.tools import ToolBox

    ws = isolate["workspace"]
    (ws / "doc.md").write_text("original", encoding="utf-8")
    j = Journal()
    box = ToolBox(j, root=ws, confirm=lambda d, x: True)
    box.invoke("file.write", {"path": "doc.md", "content": "del agente"},
               "principal")
    (ws / "doc.md").write_text("editado a mano", encoding="utf-8")

    assert cli.main(["undo", "--force"]) == 0
    assert (ws / "doc.md").read_text(encoding="utf-8") == "original"


def test_undo_de_una_accion_concreta_por_id(cli, capsys, isolate):
    from fibonacci.journal import Journal
    from fibonacci.tools import ToolBox

    ws = isolate["workspace"]
    j = Journal()
    box = ToolBox(j, root=ws, confirm=lambda d, x: True)
    r = box.invoke("file.write", {"path": "solo.txt", "content": "x"}, "principal")

    assert cli.main(["undo", r.action_id]) == 0
    assert not (ws / "solo.txt").exists()


def test_undo_de_un_id_inexistente_devuelve_1(cli, capsys):
    assert cli.main(["undo", "act_noexiste"]) == 1
    assert "desconocida" in _salida(capsys).lower()


# ===========================================================================
# tasks / resume
# ===========================================================================

def test_tasks_vacio_lo_dice(cli, capsys):
    assert cli.main(["tasks"]) == 0
    assert "Sin tareas" in _salida(capsys)


def test_tasks_lista_lo_guardado(cli, capsys):
    from fibonacci.contracts import DurableTask, Step
    from fibonacci.tasks import TaskStore

    TaskStore().save(DurableTask(goal="migrar el blog", session_id="principal",
                                 steps=[Step(description="exportar")]))

    assert cli.main(["tasks"]) == 0
    assert "migrar el blog" in _salida(capsys)


def test_tasks_pending_filtra_las_reanudables(cli, capsys):
    from fibonacci.contracts import DurableTask, Step
    from fibonacci.tasks import TaskStore

    TaskStore().save(DurableTask(goal="a medias", session_id="principal",
                                 steps=[Step(description="uno")]))
    assert cli.main(["tasks", "--pending"]) == 0
    assert _salida(capsys).strip()


def test_resume_de_una_tarea_desconocida_devuelve_1(cli, capsys):
    assert cli.main(["resume", "task_noexiste"]) == 1
    assert "desconocida" in _salida(capsys)


# ===========================================================================
# schedule
# ===========================================================================

def test_schedule_vacio_muestra_un_ejemplo(cli, capsys):
    assert cli.main(["schedule", "list"]) == 0
    assert "fib schedule add" in _salida(capsys)


def test_schedule_add_y_list(cli, capsys):
    assert cli.main(["schedule", "add", "revisa-prs",
                     "revisa mis PRs abiertos", "diario 07:00"]) == 0
    assert "programada" in _salida(capsys)

    assert cli.main(["schedule", "list"]) == 0
    salida = _salida(capsys)
    assert "revisa-prs" in salida and "diario 07:00" in salida


def test_schedule_disable_y_enable(cli, capsys):
    cli.main(["schedule", "add", "diaria", "haz algo", "diario 07:00"])
    capsys.readouterr()

    assert cli.main(["schedule", "disable", "diaria"]) == 0
    cli.main(["schedule", "list"])
    assert "desactivada" in _salida(capsys)

    assert cli.main(["schedule", "enable", "diaria"]) == 0
    cli.main(["schedule", "list"])
    assert "desactivada" not in _salida(capsys)


def test_schedule_remove(cli, capsys):
    cli.main(["schedule", "add", "efimera", "haz algo", "diario 07:00"])
    capsys.readouterr()

    assert cli.main(["schedule", "remove", "efimera"]) == 0
    assert "eliminada" in _salida(capsys)

    cli.main(["schedule", "list"])
    assert "efimera" not in _salida(capsys)


class _AgenteProgramado:
    """Lo mínimo que `Scheduler.execute` necesita."""

    def __init__(self):
        self.tools = type("_T", (), {"confirm": None})()
        self.budget = type("_B", (), {"max_usd": 1.0})()
        self.vistos = []

    def chat(self, texto, session, **kw):
        from fibonacci.agent import AgentReply

        self.vistos.append(texto)
        return AgentReply(text="hecho", cost_usd=0.0, model="falso")


def test_schedule_serve_once_sin_nada_pendiente(cli, capsys, monkeypatch):
    """Una pasada y salir: lo que necesita Termux, que mata procesos largos."""
    monkeypatch.setattr(cli, "_boot",
                        lambda cfg, vault_pass=None: _AgenteProgramado())
    cli.main(["schedule", "add", "diaria", "haz algo", "diario 07:00"])
    capsys.readouterr()

    assert cli.main(["schedule", "serve", "--once"]) == 0
    assert "Nada pendiente" in _salida(capsys)


def test_schedule_serve_once_ejecuta_lo_vencido_y_sale(cli, capsys, monkeypatch):
    from fibonacci.scheduler import Scheduler

    agente = _AgenteProgramado()
    monkeypatch.setattr(cli, "_boot", lambda cfg, vault_pass=None: agente)
    cli.main(["schedule", "add", "cada-hora", "revisa los PRs", "cada 1h"])
    capsys.readouterr()

    # La tarea vence dentro de una hora: la adelantamos.
    sch = Scheduler()
    job = sch.get("cada-hora")
    sch.store.write("UPDATE jobs SET next_run=? WHERE id=?",
                    (time.time() - 5, job.id))

    assert cli.main(["schedule", "serve", "--once"]) == 0
    assert "cada-hora" in _salida(capsys)
    assert agente.vistos == ["revisa los PRs"], "debió ejecutarla una sola vez"

    # Y no se queda dando vueltas: la siguiente pasada ya no tiene nada.
    assert cli.main(["schedule", "serve", "--once"]) == 0
    assert "Nada pendiente" in _salida(capsys)


def test_schedule_history_de_una_tarea_desconocida_devuelve_1(cli, capsys):
    assert cli.main(["schedule", "history", "fantasma"]) == 1
    assert "desconocida" in _salida(capsys)


def test_schedule_run_de_una_tarea_desconocida_devuelve_1(cli, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_boot", lambda cfg, vault_pass=None: object())
    assert cli.main(["schedule", "run", "fantasma"]) == 1
    assert "desconocida" in _salida(capsys)


def test_schedule_history_lista_las_ejecuciones(cli, capsys, monkeypatch):
    from fibonacci.scheduler import Scheduler

    cli.main(["schedule", "add", "diaria", "haz algo", "diario 07:00"])
    capsys.readouterr()

    sch = Scheduler()
    job = sch.get("diaria")

    class _Caja:
        confirm = None

    class _AgenteFalso:
        """`Scheduler.execute` sustituye `tools.confirm` y `budget.max_usd`
        mientras corre, así que el doble necesita ambos."""

        tools = _Caja()
        budget = type("_P", (), {"max_usd": 1.0})()

        def chat(self, texto, session, **kw):
            from fibonacci.agent import AgentReply

            return AgentReply(text="hecho", cost_usd=0.0, model="falso")

    sch.execute(job, _AgenteFalso())

    assert cli.main(["schedule", "history", "diaria"]) == 0
    assert "hecho" in _salida(capsys)


# ===========================================================================
# vault — `-p` para no bloquear en getpass
# ===========================================================================

def test_vault_list_vacia_explica_como_llenarla(cli, capsys):
    assert cli.main(["vault", "list", "-p", "clave-de-prueba"]) == 0
    assert "vacía" in _salida(capsys)


def test_vault_add_y_list(cli, capsys, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "el-secreto")

    assert cli.main(["vault", "add", "github", "--kind", "bearer",
                     "--hosts", "api.github.com", "-p", "clave"]) == 0
    assert "guardada" in _salida(capsys)

    assert cli.main(["vault", "list", "-p", "clave"]) == 0
    salida = _salida(capsys)
    assert "github" in salida and "api.github.com" in salida
    assert "el-secreto" not in salida, "la bóveda nunca imprime el valor"


def test_vault_avisa_si_no_hay_allowlist_de_hosts(cli, capsys, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "el-secreto")
    cli.main(["vault", "add", "suelta", "-p", "clave"])
    assert "sin restricción de host" in _salida(capsys)


def test_vault_con_contrasena_incorrecta_devuelve_1(cli, capsys, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "el-secreto")
    cli.main(["vault", "add", "github", "-p", "la-buena"])
    capsys.readouterr()

    assert cli.main(["vault", "list", "-p", "la-mala"]) == 1
    assert "incorrecta" in _salida(capsys)


def test_vault_remove_borra_una_sola_vez(cli, capsys, monkeypatch):
    """Igual que `pair --revoke`: `remove` muta y se llamaba dos veces."""
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "el-secreto")
    cli.main(["vault", "add", "github", "-p", "clave"])
    capsys.readouterr()

    assert cli.main(["vault", "remove", "github", "-p", "clave"]) == 0
    assert "eliminada" in _salida(capsys)

    cli.main(["vault", "list", "-p", "clave"])
    assert "Bóveda vacía" in _salida(capsys)


# ===========================================================================
# host
# ===========================================================================

def test_host_list_vacio_sugiere_como_anadir(cli, capsys):
    assert cli.main(["host", "list"]) == 0
    assert "fib host add" in _salida(capsys)


def test_host_add_persiste_y_avisa_del_ambito_libre(cli, capsys, isolate):
    assert cli.main(["host", "add", "prod", "--host", "prod.ejemplo.com",
                     "--user", "deploy", "--scope", "free"]) == 0
    salida = _salida(capsys)
    assert "añadido" in salida
    assert "sin preguntar" in salida, "un ámbito 'free' debe advertirse"

    guardado = json.loads((isolate["config"] / "hosts.json").read_text(encoding="utf-8"))
    assert guardado["prod"]["host"] == "prod.ejemplo.com"

    assert cli.main(["host", "list"]) == 0
    assert "deploy@prod.ejemplo.com" in _salida(capsys)


# ===========================================================================
# forge / api
# ===========================================================================

def test_forge_list_sin_herramientas_no_explota(cli, capsys):
    assert cli.main(["forge", "list"]) == 0


def test_forge_server_sin_herramientas_probadas_devuelve_1(cli, capsys):
    assert cli.main(["forge", "server"]) == 1
    assert "No hay herramientas probadas" in _salida(capsys)


def test_api_list_vacia_sugiere_como_anadir(cli, capsys):
    assert cli.main(["api", "list"]) == 0
    assert "fib api add" in _salida(capsys)


def test_api_add_con_una_spec_ilegible_devuelve_1(cli, capsys, tmp_path):
    mala = tmp_path / "mala.yaml"
    mala.write_text("info:\n  title: sin paths\n", encoding="utf-8")

    assert cli.main(["api", "add", str(mala), "--prefix", "x"]) == 1
    assert "no pude leer la spec" in _salida(capsys)


def test_api_add_registra_la_spec_para_los_proximos_arranques(cli, capsys,
                                                             isolate, tmp_path):
    spec = tmp_path / "mini.json"
    spec.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Mini", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:1/v1"}],
        "paths": {"/cosas": {"get": {"operationId": "listaCosas",
                                     "summary": "lista"}}},
    }), encoding="utf-8")

    assert cli.main(["api", "add", str(spec), "--prefix", "mini",
                     "--readonly"]) == 0
    assert "herramientas" in _salida(capsys)

    guardado = json.loads((isolate["config"] / "apis.json").read_text(encoding="utf-8"))
    assert guardado["mini"]["readonly"] is True

    assert cli.main(["api", "list"]) == 0
    assert "solo lectura" in _salida(capsys)


def test_api_add_readonly_no_lista_las_que_mutan(cli, capsys, tmp_path):
    """La lista debe coincidir con lo que de verdad se adjuntó.

    Decía "1 herramientas" y a renglón seguido listaba las tres, `DELETE`
    incluido: la cuenta era correcta y la lista mentía.
    """
    spec = tmp_path / "crm.json"
    spec.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "CRM", "version": "1"},
        "servers": [{"url": "http://127.0.0.1:1/v1"}],
        "paths": {
            "/clientes": {
                "get": {"operationId": "listaClientes", "summary": "lista"},
                "post": {"operationId": "creaCliente", "summary": "crea"}},
            "/clientes/{id}": {
                "delete": {"operationId": "borraCliente", "summary": "borra"}}},
    }), encoding="utf-8")

    assert cli.main(["api", "add", str(spec), "--prefix", "crm",
                     "--readonly"]) == 0
    salida = _salida(capsys)
    assert "listaClientes" in salida
    assert "creaCliente" not in salida, "un POST no entra en modo solo lectura"
    assert "borraCliente" not in salida, "un DELETE tampoco"

    # Sin --readonly sí deben aparecer las tres.
    assert cli.main(["api", "add", str(spec), "--prefix", "crm2"]) == 0
    completa = _salida(capsys)
    assert "creaCliente" in completa and "borraCliente" in completa


# ===========================================================================
# sync
# ===========================================================================

def test_sync_export_e_import_van_y_vuelven(cli, capsys, tmp_path):
    from fibonacci.memory import Memory

    Memory().remember(Note("dato que debe viajar", kind="fact"))

    bulto = tmp_path / "respaldo.fib"
    assert cli.main(["sync", "export", str(bulto), "-p", "clave"]) == 0
    assert "exportado" in _salida(capsys)
    assert bulto.exists()

    assert cli.main(["sync", "import", str(bulto), "-p", "clave"]) == 0
    assert "importado" in _salida(capsys)


def test_sync_import_con_contrasena_incorrecta_no_lanza(cli, capsys, tmp_path):
    """Teclear mal la contraseña es el camino habitual, no el raro: debe salir
    un mensaje, no una traza de excepción."""
    bulto = tmp_path / "respaldo.fib"
    assert cli.main(["sync", "export", str(bulto), "-p", "la-buena"]) == 0
    capsys.readouterr()

    assert cli.main(["sync", "import", str(bulto), "-p", "la-mala"]) == 1
    assert "✗" in _salida(capsys) or "contraseña" in _salida(capsys)


def test_sync_import_de_un_archivo_que_no_existe_no_lanza(cli, capsys, tmp_path):
    assert cli.main(["sync", "import", str(tmp_path / "fantasma.fib"),
                     "-p", "x"]) == 1
    assert "✗" in _salida(capsys)


def test_sync_folder_reporta_los_dispositivos(cli, capsys, tmp_path):
    carpeta = tmp_path / "compartida"
    carpeta.mkdir()
    assert cli.main(["sync", "folder", str(carpeta), "-p", "clave"]) == 0
    assert "dispositivo" in _salida(capsys)


# ===========================================================================
# serve / doctor
# ===========================================================================

def test_serve_con_una_superficie_sin_configurar_devuelve_1(cli, capsys,
                                                            monkeypatch):
    """Sin `TELEGRAM_BOT_TOKEN` no hay superficie: debe decirlo, no reventar."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert cli.main(["serve", "telegram"]) == 1
    assert "✗" in _salida(capsys) or "TELEGRAM" in _salida(capsys)


def test_doctor_reporta_sin_lanzar(cli, capsys):
    """`fib doctor` debe reportar, nunca lanzar."""
    codigo = cli.main(["doctor"])
    salida = _salida(capsys)
    assert "Fibonacci" in salida
    assert "plataforma" in salida
    assert codigo in (0, 1)


def test_doctor_con_un_proveedor_vivo_sale_con_0(cli, capsys, monkeypatch):
    """
    Una capacidad sin modelo es informe, no fallo.

    `transcribe` no está cubierta por NINGÚN perfil, así que cuando los huecos
    sumaban al código de salida `fib doctor` devolvía 1 para todo el mundo,
    siempre, incluso en una instalación sana. El código no distinguía nada y
    `fib doctor && fib "..."` —tal cual aparece en el README— nunca seguía.
    """
    from fibonacci.mesh.router import ModelMesh

    monkeypatch.setattr(ModelMesh, "diagnose", lambda self: {"ollama": True})
    assert cli.main(["doctor"]) == 0
    assert "sin cobertura" in _salida(capsys), "el hueco se sigue reportando"


def test_doctor_sobrevive_a_una_cryptography_rota(cli, capsys, monkeypatch):
    """
    `cryptography` es opcional, y una instalación rota —mezclar el paquete del
    sistema con otra versión de Python— no lanza `ImportError` sino un pánico
    de pyo3, que hereda de `BaseException` y atraviesa `except Exception`.

    Se descubrió instalando el producto de verdad: `fib doctor`, el primer
    comando que el README manda ejecutar, moría con una traza de Rust.
    """
    import builtins

    from fibonacci import crypto

    class PanicoDeRust(BaseException):
        pass

    real = builtins.__import__

    def falso(nombre, *a, **k):
        if nombre.startswith("cryptography"):
            raise PanicoDeRust("Python API call failed")
        return real(nombre, *a, **k)

    monkeypatch.setattr(builtins, "__import__", falso)

    assert crypto.aes_available() is False, "debe degradar, no propagar"
    blob = crypto.encrypt("secreto", "clave")
    assert crypto.decrypt(blob, "clave") == "secreto", "el respaldo sigue sirviendo"

    assert cli.main(["doctor"]) in (0, 1)
    assert "Fibonacci" in _salida(capsys)


def test_doctor_dice_que_nadie_responde_cuando_nadie_responde(cli, capsys,
                                                              monkeypatch):
    from fibonacci.mesh.router import ModelMesh

    monkeypatch.setattr(ModelMesh, "diagnose", lambda self: {"ollama": False})
    assert cli.main(["doctor"]) == 1
    assert "Ningún proveedor responde" in _salida(capsys)


# ===========================================================================
# do / delega — con un agente falso, sin modelo
# ===========================================================================

class _AgenteDeMentira:
    """Lo mínimo que `cmd_do` y `cmd_delega` necesitan."""

    def __init__(self, fallar: bool = False):
        self.fallar = fallar

    def plan(self, goal, session_id):
        from fibonacci.contracts import DurableTask, Step

        return DurableTask(goal=goal, session_id=session_id,
                           steps=[Step(description="uno"),
                                  Step(description="dos")])

    def advance(self, task):
        from fibonacci.contracts import TaskState

        if self.fallar:
            task.steps[0].state = TaskState.FAILED
            task.state = TaskState.FAILED
            yield task
            return
        for paso in task.steps:
            paso.output, paso.state = "hecho", TaskState.DONE
            task.cursor += 1
        task.state = TaskState.DONE
        task.result = "todo hecho"
        yield task


def test_do_recorre_los_pasos_y_guarda_la_tarea(cli, capsys, monkeypatch):
    from fibonacci.tasks import TaskStore

    monkeypatch.setattr(cli, "_boot", lambda cfg, vault_pass=None: _AgenteDeMentira())
    assert cli.main(["do", "migrar", "el", "blog"]) == 0
    salida = _salida(capsys)
    assert "2 pasos" in salida and "completada" in salida

    guardadas = TaskStore().list(limit=5)
    assert guardadas and guardadas[0].goal == "migrar el blog"


def test_do_con_un_paso_fallido_devuelve_2_y_dice_como_reanudar(cli, capsys,
                                                                monkeypatch):
    monkeypatch.setattr(cli, "_boot",
                        lambda cfg, vault_pass=None: _AgenteDeMentira(fallar=True))
    assert cli.main(["do", "algo", "imposible"]) == 2
    assert "fib resume" in _salida(capsys)
