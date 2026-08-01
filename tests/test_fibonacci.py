"""
Pruebas de Fibonacci. Cubren los cuatro diferenciadores del producto, sin
necesitar ningún modelo vivo.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from fibonacci import Catalog, Journal, Memory, TaskStore, ToolBox
from fibonacci.agent import ContextBudget, _approx_tokens
from fibonacci.contracts import (
    ActionStatus, Capability, DurableTask, Note, Skill, Step, TaskState, Turn,
)
from fibonacci.platform import detect


# ---------------------------------------------------------------------------
# 1. UNDO — la razón de existir del producto
# ---------------------------------------------------------------------------

def _box(tmp):
    j = Journal(tmp / "j.db", snapshots=tmp / "snaps")
    return j, ToolBox(j, root=tmp / "ws")


def test_undo_restaura_contenido_previo(tmp_path):
    j, box = _box(tmp_path)
    ws = tmp_path / "ws"
    (ws / "notas.txt").write_text("versión original", encoding="utf-8")

    box.invoke("file.write", {"path": "notas.txt", "content": "pisado"}, "s1")
    assert (ws / "notas.txt").read_text(encoding="utf-8") == "pisado"

    ok, msg = j.undo_last("s1")
    assert ok, msg
    assert (ws / "notas.txt").read_text(encoding="utf-8") == "versión original"


def test_undo_de_archivo_nuevo_lo_elimina(tmp_path):
    j, box = _box(tmp_path)
    box.invoke("file.write", {"path": "sub/nuevo.md", "content": "hola"}, "s1")
    assert (tmp_path / "ws/sub/nuevo.md").exists()

    ok, _ = j.undo_last("s1")
    assert ok and not (tmp_path / "ws/sub/nuevo.md").exists()


def test_undo_de_borrado_recupera_el_archivo(tmp_path):
    j, box = _box(tmp_path)
    (tmp_path / "ws/importante.txt").write_text("no me borres", encoding="utf-8")

    box.invoke("file.delete", {"path": "importante.txt"}, "s1")
    assert not (tmp_path / "ws/importante.txt").exists()

    ok, _ = j.undo_last("s1")
    assert ok
    assert (tmp_path / "ws/importante.txt").read_text(encoding="utf-8") == "no me borres"


def test_undo_de_sesion_revierte_en_orden_inverso(tmp_path):
    """Varias acciones encadenadas se deshacen como una transacción."""
    j, box = _box(tmp_path)
    ws = tmp_path / "ws"
    (ws / "a.txt").write_text("A0", encoding="utf-8")

    box.invoke("file.write", {"path": "a.txt", "content": "A1"}, "s1")
    box.invoke("file.write", {"path": "b.txt", "content": "B1"}, "s1")
    box.invoke("file.move", {"src": "b.txt", "dst": "c.txt"}, "s1")

    n, _ = j.undo_session("s1")
    assert n == 3
    assert (ws / "a.txt").read_text(encoding="utf-8") == "A0"
    assert not (ws / "b.txt").exists() and not (ws / "c.txt").exists()


def test_herramienta_mutante_sin_undo_no_se_registra(tmp_path):
    """Invariante de API: obliga a pensar en reversibilidad al contribuir."""
    from fibonacci.contracts import ToolSpec

    j, box = _box(tmp_path)
    with pytest.raises(ValueError, match="undo"):
        box.register(
            ToolSpec("db.wipe", "borra todo", {"type": "object", "properties": {}},
                     mutating=True),
            lambda: "listo",
        )


def test_irreversible_exige_confirmacion(tmp_path):
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    negado = ToolBox(j, root=tmp_path / "ws", confirm=lambda desc, danger: False)
    r = negado.invoke("shell.run", {"command": "echo hola"}, "s1")
    assert not r.ok and r.needs_confirmation

    aceptado = ToolBox(j, root=tmp_path / "ws", confirm=lambda desc, danger: True)
    r = aceptado.invoke("shell.run", {"command": "echo hola"}, "s1")
    assert r.ok and "hola" in r.content


def test_shell_queda_marcado_irreversible(tmp_path):
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    box.invoke("shell.run", {"command": "echo x"}, "s1")
    assert j.history("s1")[0].status == ActionStatus.IRREVERSIBLE


def test_area_de_trabajo_bloquea_traversal(tmp_path):
    _, box = _box(tmp_path)
    r = box.invoke("file.read", {"path": "../../../etc/passwd"}, "s1")
    assert not r.ok


def test_calc_no_ejecuta_codigo(tmp_path):
    _, box = _box(tmp_path)
    assert not box.invoke("calc", {"expression": "__import__('os').system('ls')"},
                          "s1").ok
    assert "42" in box.invoke("calc", {"expression": "6*7"}, "s1").content


# ---------------------------------------------------------------------------
# 2. MEMORIA — decaimiento y contradicción
# ---------------------------------------------------------------------------

def test_confianza_decae_con_el_tiempo():
    vieja = Note("trabaja en Acme", confidence=0.9, half_life_days=180)
    vieja.ts = time.time() - 365 * 86400
    fresca = Note("trabaja en Beta", confidence=0.9, half_life_days=180)
    assert vieja.current_confidence() < 0.3
    assert fresca.current_confidence() > 0.85
    assert vieja.stale and not fresca.stale


def test_dato_permanente_no_decae():
    n = Note("se llama Héctor", confidence=0.95, half_life_days=0)
    n.ts = time.time() - 3650 * 86400
    assert n.current_confidence() == 0.95


def test_contradiccion_se_marca_en_vez_de_coexistir(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.remember(Note("el proyecto principal usa PostgreSQL como base", kind="project"))
    _, conflicts = m.remember(Note("el proyecto principal usa MongoDB como base",
                                   kind="project"))
    assert conflicts, "debió detectar la contradicción"
    assert len(m.open_conflicts()) == 1


def test_resolver_contradiccion_retira_la_perdedora(tmp_path):
    m = Memory(tmp_path / "m.db")
    a = Note("vive en Guadalajara ahora mismo", kind="fact")
    b = Note("vive en Monterrey ahora mismo", kind="fact")
    m.remember(a)
    m.remember(b)
    m.resolve_conflict(keep_id=b.id, drop_id=a.id)
    contents = [n.content for n in m.recall_all()]
    assert b.content in contents and a.content not in contents


def test_purga_de_notas_caducas(tmp_path):
    m = Memory(tmp_path / "m.db")
    n = Note("dato efímero", confidence=0.5, half_life_days=7)
    n.ts = time.time() - 200 * 86400
    m.remember(n, detect_conflicts=False)
    assert m.forget_stale() == 1
    assert not m.recall_all()


# ---------------------------------------------------------------------------
# 3. SKILLS — se ganan la activación
# ---------------------------------------------------------------------------

def test_skill_nueva_no_entra_al_prompt(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.save_skill(Skill(name="ordenar-descargas", body="...", triggers=["descargas"],
                       status="candidate"))
    assert m.select_skills("ordena mis descargas") == []


def test_skill_asciende_con_evidencia(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.save_skill(Skill(name="respaldo", body="...", triggers=["respaldo"],
                       status="candidate"))
    for _ in range(3):
        m.score_skill("respaldo", True)
    assert m.skills()[0].status == "shadow"
    for _ in range(5):
        m.score_skill("respaldo", True)
    assert m.skills()[0].status == "active"
    assert m.select_skills("hazme un respaldo")


def test_skill_mala_se_retira_sola(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.save_skill(Skill(name="mala", body="...", triggers=["x"], status="shadow",
                       trials=7, wins=1))
    m.score_skill("mala", False)
    assert m.skills()[0].status == "retired"
    assert m.select_skills("x") == []


def test_skill_activa_que_empeora_tambien_se_retira(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.save_skill(Skill(name="degradada", body="...", triggers=["y"],
                       status="active", trials=5, wins=1))
    m.score_skill("degradada", False)
    assert m.skills()[0].status == "retired"


# ---------------------------------------------------------------------------
# 4. PORTABILIDAD Y CONTEXTO
# ---------------------------------------------------------------------------

def test_plataforma_se_detecta_y_da_rutas(isolate):
    """
    Las rutas se piden al módulo, no a los símbolos importados arriba.

    `isolate` parchea `fibonacci.platform`, pero un `from ... import data_dir`
    copia la referencia y se le escapa. Esta prueba llamaba a las funciones de
    verdad, y como crean el directorio si no existe, **ensuciaba el home real
    de quien corriera pytest**. Se descubrió en el job de CI sin red, que corre
    como un usuario sin permiso de escritura ahí.
    """
    import fibonacci.platform as plat

    p = detect()
    assert p.os in ("linux", "macos", "windows", "android", "bsd")
    assert plat.data_dir().exists() and plat.config_dir().exists()
    assert plat.data_dir() == isolate["data"], "debe apuntar al tmp de la prueba"


def test_presupuesto_reserva_espacio_para_la_respuesta():
    b = ContextBudget(total=32_768)
    assert b.usable < b.total
    partes = b.slice(b.share_system) + b.slice(b.share_memory) + b.slice(b.share_history)
    assert partes < b.usable, "el reparto nunca debe llenar la ventana"


def test_estimador_de_tokens_es_razonable():
    assert 200 < _approx_tokens("hola mundo " * 100) < 500


def test_catalogo_cubre_lo_esencial_en_modo_local():
    cat = Catalog.from_profile("local")
    for cap in (Capability.CHAT, Capability.REASONING, Capability.CODE,
                Capability.EMBEDDING):
        assert cat.find(cap, local_only=True), f"sin cobertura local: {cap}"


def test_critico_puede_ser_de_otra_familia():
    cat = Catalog.from_profile("local")
    ejecutor = cat.find(Capability.REASONING, local_only=True)[0]
    assert cat.find(Capability.CRITIQUE, local_only=True, exclude={ejecutor.id})


# ---------------------------------------------------------------------------
# 5. TAREAS DURABLES
# ---------------------------------------------------------------------------

def test_tarea_sobrevive_al_proceso(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    t = DurableTask(goal="migrar el blog", session_id="s1",
                    steps=[Step(description="exportar"), Step(description="convertir"),
                           Step(description="publicar")])
    t.steps[0].state = TaskState.DONE
    t.cursor = 1
    t.state = TaskState.RUNNING
    store.save(t)

    otro = TaskStore(tmp_path / "t.db")        # como si fuera otro dispositivo
    recuperada = otro.get(t.id)
    assert recuperada.cursor == 1
    assert recuperada.progress == (1, 3)
    assert recuperada.id in {x.id for x in otro.resumable()}


def test_tarea_terminada_no_aparece_como_reanudable(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    t = DurableTask(goal="listo", session_id="s1", steps=[Step(description="x")])
    t.state = TaskState.DONE
    store.save(t)
    assert not store.resumable()


# ---------------------------------------------------------------------------
# 6. SESIONES Y MCP
# ---------------------------------------------------------------------------

def test_continuidad_entre_superficies(tmp_path):
    """La misma clave de sesión = mismo historial desde CLI o teléfono."""
    m = Memory(tmp_path / "m.db")
    m.add_turn(Turn(session_id="casa", user="hola desde la laptop",
                    assistant="hey", surface="cli"))
    m.add_turn(Turn(session_id="casa", user="sigo desde el teléfono",
                    assistant="ok", surface="telegram"))
    turnos = m.session("casa")
    assert len(turnos) == 2
    assert {t.surface for t in turnos} == {"cli", "telegram"}


def test_busqueda_en_conversaciones_pasadas(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.add_turn(Turn(session_id="s1", user="cómo configuro nginx con certbot",
                    assistant="usa certbot --nginx"))
    m.add_turn(Turn(session_id="s2", user="receta de pozole", assistant="..."))
    hits = m.search_turns("certbot")
    assert hits and "nginx" in hits[0].user


def test_mcp_expone_undo():
    """Ningún otro servidor MCP ofrece esto."""
    from fibonacci.mcp import TOOLS, MCPServer

    names = {t["name"] for t in TOOLS}
    assert "fibonacci_undo" in names and "fibonacci_do" in names

    s = MCPServer()
    init = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "fibonacci"
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_journal_reporta_cobertura_de_undo(tmp_path):
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    box.invoke("file.write", {"path": "a.txt", "content": "1"}, "s1")
    box.invoke("file.write", {"path": "b.txt", "content": "2"}, "s1")
    box.invoke("shell.run", {"command": "echo x"}, "s1")
    st = j.stats()
    assert st["acciones"] == 3 and st["irreversibles"] == 1
    assert st["cobertura_undo"] == "67%"
