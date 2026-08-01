"""Pruebas de la capa de APIs (bóveda, cliente, OpenAPI) y las primitivas."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from fibonacci.api import (
    ApiClient, Credential, OpenApiSpec, Vault, _mini_yaml,
)
from fibonacci.journal import Journal
from fibonacci.primitives import (
    Budget, Checkpoint, Fallback, Gate, Observe, Race, Retry,
    Verify, resilient, robust, transactional,
)
from fibonacci.tools import ToolBox
from fibonacci.tools_api import attach_api, attach_openapi, attach_primitives


# ===========================================================================
# Bóveda
# ===========================================================================

def _vault(tmp):
    v = Vault(tmp / "v.enc")
    v.unlock("clave-maestra")
    return v


def test_credencial_se_guarda_cifrada(tmp_path):
    v = _vault(tmp_path)
    v.put(Credential(name="gh", kind="bearer", secret="TOKEN-SECRETO-123"))
    crudo = (tmp_path / "v.enc").read_text()
    assert "TOKEN-SECRETO-123" not in crudo, "el secreto no debe estar en claro"


def test_boveda_se_reabre_con_la_clave(tmp_path):
    v = _vault(tmp_path)
    v.put(Credential(name="gh", secret="s3cr3t"))
    otra = Vault(tmp_path / "v.enc")
    assert otra.unlock("clave-maestra")
    assert otra.get("gh").secret == "s3cr3t"


def test_clave_incorrecta_no_abre(tmp_path):
    v = _vault(tmp_path)
    v.put(Credential(name="gh", secret="s"))
    assert not Vault(tmp_path / "v.enc").unlock("otra-clave")


def test_describe_nunca_expone_valores(tmp_path):
    """El modelo puede saber qué credenciales hay, jamás su valor."""
    v = _vault(tmp_path)
    v.put(Credential(name="gh", secret="TOKEN-SECRETO", host_allowlist=["api.github.com"]))
    texto = json.dumps(v.describe())
    assert "TOKEN-SECRETO" not in texto
    assert "gh" in texto and "api.github.com" in texto


def test_credencial_atada_a_host(tmp_path):
    c = Credential(name="x", secret="s", host_allowlist=["api.ejemplo.com"])
    assert c.applies_to("https://api.ejemplo.com/v1/x")
    assert c.applies_to("https://sub.api.ejemplo.com/x")
    assert not c.applies_to("https://evil.com/x")


def test_credencial_sin_allowlist_aplica_a_todo(tmp_path):
    assert Credential(name="x", secret="s").applies_to("https://cualquiera.com")


def test_eliminar_credencial(tmp_path):
    v = _vault(tmp_path)
    v.put(Credential(name="tmp", secret="s"))
    assert v.remove("tmp") and not v.remove("tmp")
    assert "tmp" not in v.names()


# ===========================================================================
# Cliente HTTP contra un servidor real
# ===========================================================================

class _Handler(BaseHTTPRequestHandler):
    recibidas: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        _Handler.recibidas.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/openapi"):
            self._send(_SPEC)
        elif self.path.startswith("/clientes/"):
            self._send({"id": self.path.rsplit("/", 1)[-1]})
        elif self.path.startswith("/falla"):
            self._send({"error": "boom"}, 500)
        else:
            self._send({"ok": True, "ruta": self.path})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(n)
        _Handler.recibidas.append(("POST", self.path, dict(self.headers)))
        self._send({"creado": True, "recibi": json.loads(cuerpo or b"{}")}, 201)


_SPEC = {
    "openapi": "3.0.0", "info": {"title": "Demo"},
    "servers": [{"url": "http://127.0.0.1:8901"}],
    "paths": {
        "/clientes": {
            "get": {"operationId": "listaClientes", "summary": "lista",
                    "parameters": [{"name": "limit", "in": "query",
                                    "schema": {"type": "integer"}}]},
            "post": {"operationId": "creaCliente", "summary": "crea",
                     "requestBody": {"required": True}}},
        "/clientes/{id}": {
            "get": {"operationId": "verCliente", "summary": "detalle",
                    "parameters": [{"name": "id", "in": "path", "required": True,
                                    "schema": {"type": "string"}}]}}},
}

_SRV = None
BASE = "http://127.0.0.1:8901"


def _server():
    global _SRV
    if _SRV is None:
        _SRV = ThreadingHTTPServer(("127.0.0.1", 8901), _Handler)
        threading.Thread(target=_SRV.serve_forever, daemon=True).start()
        time.sleep(0.2)
    _Handler.recibidas.clear()
    return _SRV


def test_get_basico(tmp_path):
    _server()
    r = ApiClient(_vault(tmp_path)).request("GET", f"{BASE}/hola")
    assert r.ok and r.json()["ok"]


def test_credencial_se_inyecta_pero_no_vuelve(tmp_path):
    """El servidor recibe el token; el resumen para el modelo no lo contiene."""
    _server()
    v = _vault(tmp_path)
    v.put(Credential(name="api", kind="bearer", secret="TOKEN-XYZ",
                     host_allowlist=["127.0.0.1"]))
    r = ApiClient(v).request("GET", f"{BASE}/hola", credential="api")
    assert "Bearer TOKEN-XYZ" in str(_Handler.recibidas[-1][2])
    assert "TOKEN-XYZ" not in r.summarize()


def test_credencial_no_va_a_host_ajeno(tmp_path):
    v = _vault(tmp_path)
    v.put(Credential(name="api", secret="s", host_allowlist=["127.0.0.1"]))
    with pytest.raises(PermissionError):
        ApiClient(v).request("GET", "https://evil.com/x", credential="api")


def test_credencial_inexistente_da_error_util(tmp_path):
    with pytest.raises(ValueError, match="no existe"):
        ApiClient(_vault(tmp_path)).request("GET", f"{BASE}/x", credential="fantasma")


def test_error_http_no_lanza_excepcion(tmp_path):
    """Un 500 es información para el agente, no una excepción que lo tumbe."""
    _server()
    r = ApiClient(_vault(tmp_path)).request("GET", f"{BASE}/falla")
    assert not r.ok and r.status == 500 and "boom" in r.body


def test_post_con_cuerpo_json(tmp_path):
    _server()
    r = ApiClient(_vault(tmp_path)).request(
        "POST", f"{BASE}/clientes", body={"nombre": "ACME"})
    assert r.status == 201 and r.json()["recibi"]["nombre"] == "ACME"


def test_auth_basic(tmp_path):
    _server()
    v = _vault(tmp_path)
    v.put(Credential(name="b", kind="basic", username="u", secret="p"))
    ApiClient(v).request("GET", f"{BASE}/x", credential="b")
    assert "Basic" in str(_Handler.recibidas[-1][2])


def test_auth_por_query(tmp_path):
    _server()
    v = _vault(tmp_path)
    v.put(Credential(name="q", kind="query", secret="K123", query_param="api_key"))
    ApiClient(v).request("GET", f"{BASE}/x", credential="q")
    assert "api_key=K123" in _Handler.recibidas[-1][1]


# ===========================================================================
# OpenAPI
# ===========================================================================

def test_spec_desde_url_genera_endpoints():
    _server()
    spec = OpenApiSpec.from_url(f"{BASE}/openapi.json")
    ids = {e.operation_id for e in spec.endpoints()}
    assert ids == {"listaClientes", "creaCliente", "verCliente"}


def test_endpoints_mutantes_se_marcan():
    _server()
    spec = OpenApiSpec.from_url(f"{BASE}/openapi.json")
    por_id = {e.operation_id: e for e in spec.endpoints()}
    assert por_id["creaCliente"].mutating
    assert not por_id["verCliente"].mutating


def test_excluir_mutantes():
    _server()
    spec = OpenApiSpec.from_url(f"{BASE}/openapi.json")
    ids = {e.operation_id for e in spec.endpoints(include_mutating=False)}
    assert "creaCliente" not in ids and "verCliente" in ids


def test_openapi_se_convierte_en_herramientas(tmp_path):
    _server()
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    client = attach_api(box, _vault(tmp_path))
    spec = OpenApiSpec.from_url(f"{BASE}/openapi.json")
    n = attach_openapi(box, spec, client, prefix="demo")
    assert n == 3
    nombres = {s.name for s in box.specs()}
    assert "api.demo.verCliente" in nombres


def test_parametro_de_ruta_se_sustituye(tmp_path):
    _server()
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    client = attach_api(box, _vault(tmp_path))
    attach_openapi(box, OpenApiSpec.from_url(f"{BASE}/openapi.json"), client,
                   prefix="demo")
    r = box.invoke("api.demo.verCliente", {"id": "42"}, "s1")
    assert r.ok and "42" in r.content


def test_falta_parametro_de_ruta_avisa(tmp_path):
    _server()
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    client = attach_api(box, _vault(tmp_path))
    attach_openapi(box, OpenApiSpec.from_url(f"{BASE}/openapi.json"), client,
                   prefix="demo")
    r = box.invoke("api.demo.verCliente", {}, "s1")
    assert "Faltan parámetros" in r.content


def test_llamada_mutante_queda_irreversible(tmp_path):
    """Un POST a un servicio externo no tiene undo: se registra como tal."""
    _server()
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    attach_api(box, _vault(tmp_path))
    box.invoke("api.call", {"method": "POST", "url": f"{BASE}/clientes",
                            "body": {"x": 1}}, "s1")
    assert any(a.status.value == "irreversible" for a in j.history("s1"))


def test_yaml_minimo_parsea_paths():
    spec = _mini_yaml("""
openapi: 3.0.0
info:
  title: Mini
paths:
  /uno:
    get:
      operationId: getUno
""")
    assert "paths" in spec and "/uno" in spec["paths"]


def test_yaml_sin_paths_falla_con_mensaje_util():
    with pytest.raises(ValueError, match="JSON"):
        _mini_yaml("info:\n  title: nada\n")


# ===========================================================================
# Primitivas
# ===========================================================================

def test_retry_tiene_exito_tras_fallos():
    n = [0]

    def flaky():
        n[0] += 1
        if n[0] < 3:
            raise RuntimeError("transitorio")
        return "listo"

    r = Retry(3, base_delay=0.01).run(flaky)
    assert r.ok and r.value == "listo" and r.attempts == 3


def test_retry_se_rinde():
    r = Retry(2, base_delay=0.01).run(
        lambda: (_ for _ in ()).throw(RuntimeError("siempre")))
    assert not r.ok and r.attempts == 2


def test_retry_respeta_retry_on():
    """No reintentar lo que nunca va a funcionar."""
    n = [0]

    def permanente():
        n[0] += 1
        raise ValueError("400 bad request")

    r = Retry(5, base_delay=0.01,
              retry_on=lambda e: not isinstance(e, ValueError)).run(permanente)
    assert not r.ok and n[0] == 1, "no debe reintentar un error permanente"


def test_fallback_usa_la_alternativa():
    r = Fallback([lambda: "plan B"]).run(
        lambda: (_ for _ in ()).throw(RuntimeError("A falló")))
    assert r.ok and r.value == "plan B"


def test_fallback_todas_fallan():
    r = Fallback([lambda: (_ for _ in ()).throw(RuntimeError("b"))]).run(
        lambda: (_ for _ in ()).throw(RuntimeError("a")))
    assert not r.ok and "a" in r.error and "b" in r.error


def test_verify_rechaza_resultado_invalido():
    r = Verify(lambda v: v > 100, "mayor que 100").run(lambda: 5)
    assert not r.ok and "mayor que 100" in r.error and r.value == 5


def test_verify_repara():
    r = Verify(lambda v: v > 100, repair=lambda v: v * 100).run(lambda: 5)
    assert r.ok and r.value == 500


def test_cadena_acumula_la_traza():
    r = (Retry(2, base_delay=0.01) >> Verify(lambda v: isinstance(v, str))).run(
        lambda: "texto")
    assert r.ok and r.trace == ["retry: ok", "verify: ok"]


def test_cadena_corta_al_primer_fallo():
    ejecutado = []
    r = (Verify(lambda v: False, "imposible")
         >> Verify(lambda v: ejecutado.append(1) or True)).run(lambda: 1)
    assert not r.ok and not ejecutado


def test_race_gana_el_rapido():
    r = Race([lambda: (time.sleep(0.5), "lento")[1], lambda: "rápido"]).run(None)
    assert r.ok and r.value == "rápido"


def test_race_sin_ganadores():
    r = Race([lambda: (_ for _ in ()).throw(RuntimeError("x"))], timeout=2).run(None)
    assert not r.ok


def test_budget_corta_por_tiempo():
    r = Budget(max_seconds=0.1).run(lambda: (time.sleep(2), "tarde")[1])
    assert not r.ok and "excedió" in r.error


def test_budget_corta_por_gasto():
    gasto = [0.0]
    r = Budget(max_seconds=5, max_usd=0.05,
               meter=lambda: gasto[0]).run(
        lambda: (gasto.__setitem__(0, 0.5), "hecho")[1])
    assert not r.ok and "gastó" in r.error


def test_gate_bloquea_sin_precondicion():
    ejecutado = []
    r = Gate(lambda: False, "hay respaldo").run(lambda: ejecutado.append(1))
    assert not r.ok and not ejecutado, "no debe ejecutar si el gate cierra"


def test_gate_ofrece_salida_alterna():
    r = Gate(lambda: False, "x", on_blocked=lambda: "modo seguro").run(
        lambda: "peligroso")
    assert r.ok and r.value == "modo seguro"


def test_observe_espera_a_la_condicion():
    estado = [0]

    def cond():
        estado[0] += 1
        return estado[0] >= 3

    r = Observe(cond, timeout=3, interval=0.02).run(lambda: "iniciado")
    assert r.ok and r.attempts == 3


def test_observe_expira():
    r = Observe(lambda: False, timeout=0.15, interval=0.05).run(None)
    assert not r.ok and "no se cumplió" in r.error


def test_checkpoint_y_rollback_real(tmp_path):
    """La primitiva conecta con la garantía central: reversibilidad declarada."""
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.txt").write_text("original", encoding="utf-8")

    cp = Checkpoint("antes", j, "s1")
    cp.run(lambda: "marcado")
    time.sleep(0.01)
    box.invoke("file.write", {"path": "a.txt", "content": "modificado"}, "s1")
    box.invoke("file.write", {"path": "b.txt", "content": "nuevo"}, "s1")

    hechas, _ = cp.rollback()
    assert hechas == 2
    assert (ws / "a.txt").read_text() == "original"
    assert not (ws / "b.txt").exists()


def test_receta_transactional(tmp_path):
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    chain = transactional("migracion", j, "s1", check=lambda v: v == "ok")
    r = chain.run(lambda: "ok")
    assert r.ok
    assert hasattr(chain, "checkpoint")


def test_receta_robust():
    n = [0]

    def flaky():
        n[0] += 1
        if n[0] < 2:
            raise RuntimeError("x")
        return "bueno"

    assert robust(3, check=lambda v: v == "bueno").run(flaky).ok


def test_receta_resilient():
    r = resilient([lambda: "alterna"], attempts=2).run(
        lambda: (_ for _ in ()).throw(RuntimeError("principal falló")))
    assert r.ok


# ===========================================================================
# Primitivas como herramientas del modelo
# ===========================================================================

def _box_con_flujo(tmp_path):
    j = Journal(tmp_path / "j.db", snapshots=tmp_path / "s")
    box = ToolBox(j, root=tmp_path / "ws", confirm=lambda d, x: True)
    attach_primitives(box)
    return box, j


def test_flujo_se_registra_como_herramientas(tmp_path):
    box, _ = _box_con_flujo(tmp_path)
    nombres = {s.name for s in box.specs()}
    assert {"flow.retry", "flow.fallback", "flow.race", "flow.observe"} <= nombres


def test_flow_retry_sobre_otra_herramienta(tmp_path):
    box, _ = _box_con_flujo(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "ws" / "x.txt").write_text("contenido", encoding="utf-8")
    r = box.invoke("flow.retry",
                   {"tool": "file.read", "arguments": {"path": "x.txt"},
                    "attempts": 2}, "s1")
    assert r.ok and "contenido" in r.content


def test_flow_fallback_usa_la_segunda(tmp_path):
    box, _ = _box_con_flujo(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "ws" / "existe.txt").write_text("hallado", encoding="utf-8")
    r = box.invoke("flow.fallback", {"tools": [
        {"tool": "file.read", "arguments": {"path": "no-existe.txt"}},
        {"tool": "file.read", "arguments": {"path": "existe.txt"}}]}, "s1")
    assert r.ok and "hallado" in r.content


def test_flow_no_ejecuta_codigo_arbitrario(tmp_path):
    """Las primitivas operan sobre herramientas registradas, no sobre código."""
    box, _ = _box_con_flujo(tmp_path)
    r = box.invoke("flow.retry", {"tool": "herramienta.inventada"}, "s1")
    assert "FALLÓ" in r.content or "desconocida" in r.content
