"""
FIBONACCI — APIs.

Tres capacidades que faltaban:

**1. Bóveda de credenciales.** Hasta ahora Fibonacci podía redactar secretos
que encontraba, pero no tenía dónde guardar los suyos. Sin bóveda, la única
forma de darle una API key era ponerla en el prompt o en una variable de
entorno — y lo primero la manda al modelo. Aquí las credenciales se guardan
cifradas y **nunca entran al contexto**: el agente usa `{{cred:github}}` como
referencia y la sustitución ocurre en el cliente HTTP, después de que el modelo
ya escribió la petición.

Esa inversión es lo importante. El modelo pide "llama a la API de GitHub con mi
credencial"; nunca ve el token. Aunque una inyección de prompt logre que el
agente filtre todo su contexto, el token no está ahí.

**2. Cliente HTTP completo.** GET/POST/PUT/PATCH/DELETE con cabeceras, auth y
cuerpo JSON. Las escrituras (`POST`/`PUT`/`DELETE`) se marcan mutantes y pasan
por el journal; las que el propio servicio declara reversibles pueden registrar
su inverso.

**3. Ingesta de OpenAPI.** Le das una URL de spec y Fibonacci genera una
herramienta por endpoint, con su esquema de parámetros. Es la forma más rápida
de que gane cientos de capacidades sin escribir código: apuntas a la spec de tu
CRM, tu ERP o tu propio backend y queda integrado.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ToolSpec
from .crypto import decrypt, encrypt
from .platform import config_dir

log = logging.getLogger("fibonacci.api")

CRED_REF = re.compile(r"\{\{cred:([a-zA-Z0-9_\-.]+)(?:\.(\w+))?\}\}")


# ---------------------------------------------------------------------------
# Bóveda
# ---------------------------------------------------------------------------

@dataclass
class Credential:
    name: str
    kind: str = "bearer"      # bearer | header | query | basic | raw
    secret: str = ""
    header_name: str = "Authorization"
    query_param: str = "api_key"
    username: str = ""
    host_allowlist: list[str] = field(default_factory=list)
    note: str = ""

    def applies_to(self, url: str) -> bool:
        """Una credencial atada a su host no puede filtrarse a otro dominio,
        ni siquiera si el modelo se equivoca o alguien lo engaña."""
        if not self.host_allowlist:
            return True
        host = urllib.parse.urlparse(url).hostname or ""
        return any(host == h or host.endswith("." + h) for h in self.host_allowlist)


class Vault:
    """
    Credenciales cifradas en disco. La contraseña se pide una vez por sesión y
    vive solo en memoria.
    """

    def __init__(self, path: Path | None = None, passphrase: str | None = None):
        self.path = path or (config_dir() / "vault.enc")
        self._pass = passphrase
        self._creds: dict[str, Credential] = {}
        self._loaded = False

    def unlock(self, passphrase: str) -> bool:
        self._pass = passphrase
        try:
            self._load()
            return True
        except ValueError:
            self._pass = None
            return False

    @property
    def locked(self) -> bool:
        return self._pass is None

    def _load(self) -> None:
        if self._loaded or not self.path.exists():
            self._loaded = True
            return
        if self._pass is None:
            raise ValueError("bóveda bloqueada: falta la contraseña")
        data = json.loads(decrypt(self.path.read_text(encoding="utf-8"), self._pass))
        self._creds = {k: Credential(**v) for k, v in data.items()}
        self._loaded = True

    def _save(self) -> None:
        if self._pass is None:
            raise ValueError("bóveda bloqueada")
        payload = {k: v.__dict__ for k, v in self._creds.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(encrypt(json.dumps(payload), self._pass),
                             encoding="utf-8")
        self.path.chmod(0o600)

    def put(self, cred: Credential) -> None:
        self._load()
        self._creds[cred.name] = cred
        self._save()
        log.info("Credencial '%s' guardada (%s)", cred.name, cred.kind)

    def get(self, name: str) -> Credential | None:
        self._load()
        return self._creds.get(name)

    def remove(self, name: str) -> bool:
        self._load()
        if name in self._creds:
            del self._creds[name]
            self._save()
            return True
        return False

    def names(self) -> list[str]:
        """Solo nombres. El valor nunca sale de aquí salvo para firmar una
        petición concreta."""
        self._load()
        return sorted(self._creds)

    def describe(self) -> list[dict]:
        self._load()
        return [{"nombre": c.name, "tipo": c.kind,
                 "hosts": c.host_allowlist or ["(cualquiera — considera acotarlo)"],
                 "nota": c.note}
                for c in self._creds.values()]


# ---------------------------------------------------------------------------
# Cliente HTTP
# ---------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body)

    def summarize(self, limit: int = 20_000) -> str:
        head = f"HTTP {self.status} ({self.elapsed_ms} ms)"
        body = self.body
        if len(body) > limit:
            body = body[:limit] + f"\n[...{len(self.body) - limit} caracteres omitidos]"
        return f"{head}\n\n{body}"


class ApiClient:
    """
    Cliente con sustitución de credenciales *después* de que el modelo escribe
    la petición. El token nunca pasa por el contexto.
    """

    def __init__(self, vault: Vault, timeout: float = 45.0,
                 max_bytes: int = 4_000_000):
        self.vault = vault
        self.timeout = timeout
        self.max_bytes = max_bytes

    def request(self, method: str, url: str, *,
                headers: dict[str, str] | None = None,
                body: Any = None, credential: str | None = None,
                params: dict[str, str] | None = None) -> Response:
        h = dict(headers or {})
        h.setdefault("User-Agent", "Fibonacci/0.6")
        q = dict(params or {})

        if credential:
            cred = self.vault.get(credential)
            if cred is None:
                raise ValueError(
                    f"credencial '{credential}' no existe. "
                    f"Disponibles: {', '.join(self.vault.names()) or 'ninguna'}")
            if not cred.applies_to(url):
                raise PermissionError(
                    f"la credencial '{credential}' está restringida a "
                    f"{cred.host_allowlist} y esta petición va a "
                    f"{urllib.parse.urlparse(url).hostname}")
            self._apply(cred, h, q)

        # Referencias inline {{cred:nombre}} en cabeceras o cuerpo
        h = {k: self._expand(v, url) for k, v in h.items()}
        if isinstance(body, str):
            body = self._expand(body, url)

        if q:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(q)

        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                h.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode()

        req = urllib.request.Request(url, data=data, headers=h,
                                     method=method.upper())
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(self.max_bytes).decode("utf-8", errors="replace")
                return Response(r.status, raw, dict(r.headers),
                                int((time.time() - t0) * 1000))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")[:self.max_bytes]
            return Response(e.code, raw, dict(e.headers or {}),
                            int((time.time() - t0) * 1000))

    def _apply(self, cred: Credential, headers: dict, query: dict) -> None:
        if cred.kind == "bearer":
            headers[cred.header_name] = f"Bearer {cred.secret}"
        elif cred.kind == "header":
            headers[cred.header_name] = cred.secret
        elif cred.kind == "query":
            query[cred.query_param] = cred.secret
        elif cred.kind == "basic":
            import base64
            tok = base64.b64encode(
                f"{cred.username}:{cred.secret}".encode()).decode()
            headers["Authorization"] = f"Basic {tok}"
        elif cred.kind == "raw":
            headers[cred.header_name] = cred.secret

    def _expand(self, text: str, url: str) -> str:
        def sub(m: re.Match) -> str:
            cred = self.vault.get(m.group(1))
            if cred is None:
                return m.group(0)
            if not cred.applies_to(url):
                raise PermissionError(
                    f"'{m.group(1)}' no está autorizada para este host")
            campo = m.group(2)
            return getattr(cred, campo) if campo else cred.secret
        return CRED_REF.sub(sub, text)


# ---------------------------------------------------------------------------
# Ingesta de OpenAPI
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    operation_id: str
    method: str
    path: str
    summary: str = ""
    parameters: list[dict] = field(default_factory=list)
    request_body: dict | None = None

    @property
    def mutating(self) -> bool:
        return self.method.upper() in ("POST", "PUT", "PATCH", "DELETE")


class OpenApiSpec:
    """
    Convierte una spec OpenAPI 3.x (o Swagger 2.0) en herramientas.

    Apuntas Fibonacci a la spec de tu CRM, tu ERP o tu backend y gana esos
    endpoints como capacidades, sin escribir código.
    """

    def __init__(self, spec: dict, base_url: str = ""):
        self.spec = spec
        self.base_url = base_url.rstrip("/") or self._infer_base()
        self.title = spec.get("info", {}).get("title", "api")

    @classmethod
    def from_url(cls, url: str, client: ApiClient | None = None,
                 credential: str | None = None) -> "OpenApiSpec":
        if client:
            r = client.request("GET", url, credential=credential)
            if not r.ok:
                raise ValueError(f"no se pudo leer la spec: HTTP {r.status}")
            text = r.body
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Fibonacci/0.6"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        return cls(_parse_spec(text), base_url=_base_from_url(url))

    @classmethod
    def from_file(cls, path: str | Path, base_url: str = "") -> "OpenApiSpec":
        return cls(_parse_spec(Path(path).read_text(encoding="utf-8")), base_url)

    def _infer_base(self) -> str:
        servers = self.spec.get("servers") or []
        if servers and servers[0].get("url"):
            return servers[0]["url"].rstrip("/")
        host = self.spec.get("host")
        if host:                              # Swagger 2.0
            schemes = self.spec.get("schemes") or ["https"]
            base = self.spec.get("basePath", "")
            return f"{schemes[0]}://{host}{base}".rstrip("/")
        return ""

    def endpoints(self, only: list[str] | None = None,
                  include_mutating: bool = True) -> list[Endpoint]:
        out: list[Endpoint] = []
        for path, ops in (self.spec.get("paths") or {}).items():
            if not isinstance(ops, dict):
                continue
            comunes = ops.get("parameters", [])
            for method, op in ops.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                if not isinstance(op, dict):
                    continue
                ep = Endpoint(
                    operation_id=op.get("operationId") or _slug(f"{method}_{path}"),
                    method=method.upper(), path=path,
                    summary=(op.get("summary") or op.get("description") or "")[:200],
                    parameters=list(comunes) + list(op.get("parameters", [])),
                    request_body=op.get("requestBody"))
                if not include_mutating and ep.mutating:
                    continue
                if only and ep.operation_id not in only:
                    continue
                out.append(ep)
        return out

    def to_tool_specs(self, prefix: str | None = None,
                      **kw) -> list[tuple[ToolSpec, Endpoint]]:
        pfx = prefix or _slug(self.title)
        result = []
        for ep in self.endpoints(**kw):
            props: dict[str, Any] = {}
            required: list[str] = []
            for p in ep.parameters:
                if not isinstance(p, dict) or "name" not in p:
                    continue
                esquema = p.get("schema", {}) or {}
                props[p["name"]] = {
                    "type": esquema.get("type", "string"),
                    "description": (p.get("description") or "")[:150]}
                if p.get("required"):
                    required.append(p["name"])
            if ep.request_body:
                props["body"] = {"type": "object",
                                 "description": "cuerpo JSON de la petición"}
                if ep.request_body.get("required"):
                    required.append("body")

            spec = ToolSpec(
                name=f"api.{pfx}.{ep.operation_id}",
                description=f"[{ep.method} {ep.path}] {ep.summary}",
                parameters={"type": "object", "properties": props,
                            "required": required},
                mutating=ep.mutating,
                reversible=False,            # una API externa no da undo por sí sola
                danger=2 if ep.method == "DELETE" else (1 if ep.mutating else 0))
            result.append((spec, ep))
        return result


def _parse_spec(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """
    Parser YAML mínimo para specs OpenAPI. No es completo —no lo pretende—
    pero evita añadir PyYAML como dependencia obligatoria. Si falla, el mensaje
    dice claramente que conviertas la spec a JSON.

    La validación de `paths` vive aquí y no dentro del parser propio: una spec
    sin `paths` no genera ni una herramienta, así que es un error igual de
    fatal con PyYAML instalado que sin él. Cuando la comprobación estaba solo
    en el camino manual, tener PyYAML hacía que una spec inservible pasara en
    silencio y reventara mucho más tarde, sin decir por qué.
    """
    raiz: dict | None
    try:
        import yaml           # si el usuario lo tiene, mejor
        raiz = yaml.safe_load(text)
    except ImportError:
        raiz = _yaml_a_mano(text)

    if not isinstance(raiz, dict) or not raiz.get("paths"):
        raise ValueError(
            "no pude interpretar esta spec YAML. Conviértela a JSON "
            "(o instala PyYAML) y vuelve a intentarlo.")
    return raiz


def _yaml_a_mano(text: str) -> dict:
    """Respaldo sin dependencias: sangría, mapas y listas de escalares."""
    raiz: dict = {}
    pila: list[tuple[int, Any]] = [(-1, raiz)]
    for linea in text.splitlines():
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        sangria = len(linea) - len(linea.lstrip())
        contenido = linea.strip()
        while pila and pila[-1][0] >= sangria:
            pila.pop()
        if not pila:
            break
        padre = pila[-1][1]

        if contenido.startswith("- "):
            item = contenido[2:].strip()
            if isinstance(padre, list):
                padre.append(_scalar(item))
            continue
        if ":" not in contenido:
            continue
        clave, _, valor = contenido.partition(":")
        clave, valor = clave.strip().strip('"\''), valor.strip()
        if valor:
            if isinstance(padre, dict):
                padre[clave] = _scalar(valor)
        else:
            hijo: Any = {}
            if isinstance(padre, dict):
                padre[clave] = hijo
            pila.append((sangria, hijo))
    return raiz


def _scalar(v: str):
    v = v.strip().strip('"\'')
    if v in ("true", "false"):
        return v == "true"
    if v.isdigit():
        return int(v)
    return v


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()[:40]


def _base_from_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"
