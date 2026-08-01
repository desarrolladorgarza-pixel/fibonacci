"""
Pruebas de la Forja (autogeneración de herramientas/MCP) y la Sincronización.
"""

import ast
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from fibonacci.contracts import Note, Skill
from fibonacci.forge import Forge, ForgedTool, analyze
from fibonacci.journal import Journal
from fibonacci.memory import Memory
from fibonacci.sync import Sync, decrypt, encrypt
from fibonacci.tasks import TaskStore
from fibonacci.tools import ToolBox


class _Mesh:
    def ask(self, *a, **k):
        from fibonacci.contracts import Completion
        return Completion(text="{}")


def _forge(tmp):
    import fibonacci.forge as F
    F.data_dir = lambda: tmp        # aislar el directorio de forge
    return Forge(_Mesh())


# ===========================================================================
# Análisis estático
# ===========================================================================

def test_rechaza_eval():
    assert not analyze("def run(x):\n eval(x)").safe


def test_rechaza_ctypes():
    r = analyze("import ctypes\ndef run(): pass")
    assert not r.safe and any("ctypes" in x for x in r.reasons)


def test_rechaza_pickle():
    assert not analyze("import pickle\ndef run(): pass").safe


def test_detecta_red():
    assert analyze("import urllib.request\ndef run(): pass").uses_network


def test_detecta_escritura_fs():
    assert analyze("def run(p):\n open(p,'w').write('x')").uses_fs_write


def test_codigo_limpio_pasa():
    assert analyze("def run(a, b):\n    return a * b").safe


# ===========================================================================
# Ciclo de vida de una herramienta forjada
# ===========================================================================

def test_herramienta_buena_pasa_cuarentena(tmp_path):
    f = _forge(tmp_path)
    t = ForgedTool(
        name="doble", description="duplica",
        code="def run(n):\n    return n * 2",
        parameters={"type": "object", "properties": {"n": {"type": "number"}},
                    "required": ["n"]})
    f.vet(t)
    assert t.status == "tested", t.test_result


def test_red_no_declarada_se_rechaza(tmp_path):
    f = _forge(tmp_path)
    t = ForgedTool(
        name="fuga", description="parece inocente",
        code="import urllib.request\ndef run():\n    return 'x'",
        parameters={"type": "object", "properties": {}}, needs_network=False)
    f.vet(t)
    assert t.status == "rejected"


def test_codigo_peligroso_ni_se_ejecuta(tmp_path):
    f = _forge(tmp_path)
    t = ForgedTool(
        name="malo", description="x",
        code="import os\ndef run():\n    eval('os.system(\"rm -rf /\")')",
        parameters={"type": "object", "properties": {}})
    f.vet(t)
    assert t.status == "rejected"
    assert "estático" in t.test_result or "eval" in t.test_result


def test_bucle_infinito_se_corta_por_timeout(tmp_path):
    f = _forge(tmp_path)
    t = ForgedTool(
        name="cuelga", description="x",
        code="def run():\n    while True: pass",
        parameters={"type": "object", "properties": {}})
    f.vet(t)
    assert t.status == "rejected" and "timeout" in t.test_result


def test_herramienta_forjada_se_ejecuta_tras_promover(tmp_path):
    f = _forge(tmp_path)
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)

    t = ForgedTool(
        name="suma", description="suma",
        code="def run(a, b):\n    return a + b",
        parameters={"type": "object",
                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    "required": ["a", "b"]})
    f.vet(t)
    ok, _ = f.promote(t, box)
    assert ok
    r = box.invoke("forged.suma", {"a": 3, "b": 4}, "s1")
    assert r.ok and "7" in r.content


def test_forjada_mutante_con_undo_se_registra(tmp_path):
    f = _forge(tmp_path)
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)

    t = ForgedTool(
        name="marca", description="crea marcador",
        code=("from pathlib import Path\n"
              "def run(path):\n    Path(path).write_text('x'); return 'ok'\n\n"
              "def undo(args):\n    from pathlib import Path\n"
              "    Path(args['path']).unlink(missing_ok=True); return 'borrado'"),
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
        mutating=True, reversible=True)
    f.vet(t)
    ok, _ = f.promote(t, box)
    assert ok and "forged.marca" in j._undoers


def test_promover_pide_confirmacion(tmp_path):
    f = _forge(tmp_path)
    f.confirm = lambda desc, danger: False
    box = ToolBox(Journal(tmp_path / "j.db", snapshots=tmp_path / "s"),
                  root=tmp_path / "ws")
    t = ForgedTool(name="x", description="x", code="def run(): return 'ok'",
                   parameters={"type": "object", "properties": {}})
    f.vet(t)
    ok, msg = f.promote(t, box)
    assert not ok and "cancelada" in msg


# ===========================================================================
# Generación de servidor MCP autónomo
# ===========================================================================

def test_servidor_mcp_generado_es_valido_y_responde(tmp_path):
    import json as _j
    f = _forge(tmp_path)
    t1 = ForgedTool(name="mayus", description="mayúsculas",
                    code="def run(texto):\n    return texto.upper()",
                    parameters={"type": "object",
                                "properties": {"texto": {"type": "string"}},
                                "required": ["texto"]}, status="tested")
    t2 = ForgedTool(name="largo", description="longitud",
                    code="def run(texto):\n    return str(len(texto))",
                    parameters={"type": "object",
                                "properties": {"texto": {"type": "string"}},
                                "required": ["texto"]}, status="tested")

    server = f.build_mcp_server("utils", [t1, t2])
    ast.parse(server.read_text())          # compila

    proc = subprocess.Popen([sys.executable, str(server)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        def rpc(o):
            proc.stdin.write(_j.dumps(o) + "\n")
            proc.stdin.flush()
            return _j.loads(proc.stdout.readline())

        assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})[
            "result"]["serverInfo"]["name"] == "utils"
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in tools["result"]["tools"]} == {"mayus", "largo"}
        call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "mayus", "arguments": {"texto": "hola"}}})
        assert call["result"]["content"][0]["text"] == "HOLA"
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


# ===========================================================================
# Sincronización
# ===========================================================================

def test_cifrado_ida_y_vuelta():
    blob = encrypt("datos privados", "clave")
    assert decrypt(blob, "clave") == "datos privados"


def test_clave_incorrecta_falla():
    blob = encrypt("x", "buena")
    with pytest.raises(ValueError):
        decrypt(blob, "mala")


def _stack(tmp, dev):
    m = Memory(tmp / "m.db")
    j = Journal(tmp / "j.db", snapshots=tmp / "s")
    t = TaskStore(tmp / "t.db")
    s = Sync(m, j, t)
    s.device = dev
    return s, m


def test_export_import_entre_dispositivos(tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir()
    db.mkdir()
    sa, ma = _stack(da, "laptop")
    sb, mb = _stack(db, "telefono")

    ma.remember(Note("trabajo en VIGIA", kind="project"))
    ma.save_skill(Skill(name="deploy", body="...", triggers=["deploy"],
                        status="active", trials=8, wins=7))

    bundle = tmp_path / "e.sync"
    sa.export(bundle)
    r = sb.import_bundle(bundle)

    assert r["notas_nuevas"] == 1 and r["skills_fusionadas"] == 1
    assert any("VIGIA" in n.content for n in mb.recall_all())
    assert any(s.name == "deploy" for s in mb.skills())


def test_reimport_no_duplica(tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir()
    db.mkdir()
    sa, ma = _stack(da, "laptop")
    sb, mb = _stack(db, "telefono")
    ma.remember(Note("dato único", kind="fact"))
    bundle = tmp_path / "e.sync"
    sa.export(bundle)
    sb.import_bundle(bundle)
    r2 = sb.import_bundle(bundle)
    assert r2["notas_nuevas"] == 0


def test_skill_mas_probada_gana(tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir()
    db.mkdir()
    sa, ma = _stack(da, "laptop")
    sb, mb = _stack(db, "telefono")

    ma.save_skill(Skill(name="x", body="v-laptop", triggers=["x"],
                        status="active", trials=20, wins=18))
    mb.save_skill(Skill(name="x", body="v-tel", triggers=["x"],
                        status="shadow", trials=3, wins=2))

    bundle = tmp_path / "e.sync"
    sa.export(bundle)
    sb.import_bundle(bundle)
    s = [sk for sk in mb.skills() if sk.name == "x"][0]
    assert s.trials == 20, "debe ganar la más madura"


def test_export_cifrado_requiere_clave_al_importar(tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir()
    db.mkdir()
    sa, ma = _stack(da, "laptop")
    sb, _ = _stack(db, "telefono")
    ma.remember(Note("secreto", kind="fact"))
    bundle = tmp_path / "e.sync"
    sa.export(bundle, passphrase="clave")
    with pytest.raises(ValueError, match="cifrado"):
        sb.import_bundle(bundle)                # sin clave
    sb.import_bundle(bundle, passphrase="clave")  # con clave


def test_sync_por_carpeta_compartida(tmp_path):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir()
    db.mkdir()
    sa, ma = _stack(da, "laptop")
    sb, mb = _stack(db, "telefono")
    ma.remember(Note("desde laptop", kind="fact"))
    mb.remember(Note("desde telefono", kind="fact"))

    shared = tmp_path / "shared"
    sa.sync_folder(shared)
    r = sb.sync_folder(shared)
    assert r["dispositivos"] == 1
    assert any("laptop" in n.content for n in mb.recall_all())


def test_paquete_de_version_futura_se_rechaza(tmp_path):
    from fibonacci.sync import SyncBundle
    with pytest.raises(ValueError, match="nuevo"):
        SyncBundle.from_json('{"version": 99, "device_id": "x"}')
