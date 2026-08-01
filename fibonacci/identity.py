"""
FIBONACCI — Identidad y ámbitos.

Este módulo es lo que permite dar MÁS autonomía, no menos.

El error de diseño frecuente es tratar la autonomía como un interruptor
global: "pregunta todo" o "no preguntes nada". Ambos extremos son malos. El
primero cansa hasta que el usuario aprueba sin leer; el segundo obliga a
encerrar al agente en una jaula donde no sirve.

Fibonacci lo modela como **ámbitos**. Tú declaras dónde opera libre:

    ~/proyectos/**        → libre, sin preguntar nunca
    staging.midominio.com → root, libre
    /etc/**               → prohibido
    producción            → siempre confirmar

Dentro de su ámbito el agente es completamente autónomo: no interrumpe, no
pide permiso, no se detiene. Fuera, se detiene. Eso produce más trabajo
autónomo real que un "sí a todo", porque un "sí a todo" solo puede correr en
una VM de juguete.

## Principals

Un `Principal` es quien pide. El dueño de la máquina no es lo mismo que un
desconocido que escribió al bot de Telegram. Sin esta distinción, exponer una
superficie de mensajería equivale a dar shell a internet.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from .platform import config_dir

log = logging.getLogger("fibonacci.identity")


class Trust(IntEnum):
    """Cuánto se le concede a quien pide."""

    UNKNOWN = 0     # no puede nada. Default para quien llega sin emparejar.
    GUEST = 1       # solo lectura y consulta. Nada que mute el mundo.
    MEMBER = 2      # opera dentro de ámbitos libres.
    OWNER = 3       # todo, con las reglas de ámbito que él mismo puso.


@dataclass
class Principal:
    """Quien hace la petición."""

    id: str                       # "cli:local" | "telegram:5512345" | "mcp:claude"
    display: str = ""
    trust: Trust = Trust.UNKNOWN
    surface: str = "cli"
    paired_at: float = 0.0
    last_seen: float = 0.0

    @property
    def can_mutate(self) -> bool:
        return self.trust >= Trust.MEMBER


class Decision(IntEnum):
    ALLOW = 0       # adelante, sin preguntar
    CONFIRM = 1     # requiere sí explícito del humano
    DENY = 2        # no, y no hay confirmación que lo cambie


@dataclass
class Scope:
    """
    Una regla de ámbito. El orden importa: gana la primera que coincida, así
    que las prohibiciones van arriba.
    """

    pattern: str                  # glob de ruta, host, o "tool:nombre"
    decision: Decision
    min_trust: Trust = Trust.MEMBER
    note: str = ""

    def matches(self, target: str) -> bool:
        t = str(target).replace("\\", "/")
        return fnmatch.fnmatch(t, self.pattern)


# Reglas base. Se pueden ampliar, pero las DENY del núcleo no se quitan por
# configuración: si alguien quiere que su agente borre /etc, que edite el
# código a conciencia y sepa lo que hace.
CORE_DENY = [
    Scope("/etc/**", Decision.DENY, note="configuración del sistema"),
    Scope("/boot/**", Decision.DENY, note="arranque"),
    Scope("/sys/**", Decision.DENY),
    Scope("/proc/**", Decision.DENY),
    Scope("C:/Windows/**", Decision.DENY),
    Scope("**/.ssh/**", Decision.DENY, note="llaves SSH"),
    Scope("**/.aws/credentials", Decision.DENY),
    Scope("**/.git-credentials", Decision.DENY),
    Scope("**/id_rsa*", Decision.DENY),
]

DEFAULT_SCOPES = [
    Scope("~/proyectos/**", Decision.ALLOW, note="área de trabajo libre"),
    Scope("~/fibonacci/**", Decision.ALLOW, note="workspace del agente"),
    Scope("/tmp/**", Decision.ALLOW),
    Scope("**/producci*n/**", Decision.CONFIRM, Trust.OWNER, "producción"),
    Scope("**/prod/**", Decision.CONFIRM, Trust.OWNER),
]


@dataclass
class Authority:
    """
    Resuelve: ¿este principal puede hacer esta acción sobre este objetivo?

    Devuelve ALLOW / CONFIRM / DENY y el motivo. El motivo se muestra siempre:
    un agente que se niega sin explicar es imposible de configurar bien.
    """

    scopes: list[Scope] = field(default_factory=lambda: list(CORE_DENY) + list(DEFAULT_SCOPES))
    principals: dict[str, Principal] = field(default_factory=dict)
    path: Path | None = None
    # Códigos de emparejamiento vivos. Nunca se persisten: un código que
    # sobrevive a un reinicio es un código que alguien puede encontrar.
    _pending: dict[str, float] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Authority":
        p = path or (config_dir() / "authority.json")
        auth = cls(path=p)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.error("authority.json ilegible; usando reglas base")
                return auth
            user_scopes = [
                Scope(s["pattern"], Decision(s["decision"]),
                      Trust(s.get("min_trust", 2)), s.get("note", ""))
                for s in data.get("scopes", [])
            ]
            # Las DENY del núcleo siempre van primero y no se pueden anular.
            auth.scopes = list(CORE_DENY) + user_scopes
            auth.principals = {
                k: Principal(id=k, display=v.get("display", ""),
                             trust=Trust(v.get("trust", 0)),
                             surface=v.get("surface", "cli"),
                             paired_at=v.get("paired_at", 0.0))
                for k, v in data.get("principals", {}).items()
            }
        else:
            # Primer arranque: quien está en la terminal local es el dueño.
            auth.principals["cli:local"] = Principal(
                "cli:local", "dueño local", Trust.OWNER, "cli", time.time())
            auth.save()
        return auth

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        custom = [s for s in self.scopes if s not in CORE_DENY]
        self.path.write_text(json.dumps({
            "scopes": [{"pattern": s.pattern, "decision": int(s.decision),
                        "min_trust": int(s.min_trust), "note": s.note}
                       for s in custom],
            "principals": {k: {"display": p.display, "trust": int(p.trust),
                               "surface": p.surface, "paired_at": p.paired_at}
                           for k, p in self.principals.items()},
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------

    def principal(self, pid: str) -> Principal:
        p = self.principals.get(pid)
        if p is None:
            # Desconocido = sin permisos. Nunca al revés.
            p = Principal(id=pid, trust=Trust.UNKNOWN)
        p.last_seen = time.time()
        return p

    def pair(self, pid: str, code: str, display: str = "",
             trust: Trust = Trust.MEMBER) -> bool:
        """Emparejamiento con código de un solo uso, generado en la terminal."""
        pending = self._pending.get(code)
        if not pending or pending < time.time():
            return False
        self._pending.pop(code, None)
        self.principals[pid] = Principal(pid, display or pid, trust,
                                         pid.split(":")[0], time.time())
        self.save()
        log.info("Emparejado %s con confianza %s", pid, trust.name)
        return True

    def new_pairing_code(self, ttl: float = 300.0) -> str:
        ahora = time.time()
        # Higiene: los vencidos se van al generar uno nuevo.
        self._pending = {k: v for k, v in self._pending.items() if v > ahora}
        code = "-".join(secrets.token_hex(2) for _ in range(3))
        self._pending[code] = ahora + ttl
        return code

    def revoke(self, pid: str) -> bool:
        if pid in self.principals:
            del self.principals[pid]
            self.save()
            return True
        return False

    # ------------------------------------------------------------------

    def check(self, principal: Principal, action: str,
              target: str | None = None) -> tuple[Decision, str]:
        """
        La pregunta central del sistema. `action` es el nombre de herramienta,
        `target` la ruta, host o recurso afectado.
        """
        if principal.trust == Trust.UNKNOWN:
            return Decision.DENY, (
                f"'{principal.id}' no está emparejado. Ejecuta `fib pair` en la "
                "terminal para generar un código.")

        mutating = _is_mutating(action)

        # Un invitado no muta nada sin tu visto bueno, y tampoco lee libremente:
        # mostrarle el contenido de tus archivos ya es una divulgación. En
        # 0.2.x esto era incoherente — pedía confirmación para leer dentro de un
        # ámbito declarado pero permitía leer fuera de él, justo al revés.
        if principal.trust <= Trust.GUEST:
            return Decision.CONFIRM, (
                f"'{principal.display or principal.id}' es invitado: toda acción "
                "suya requiere tu confirmación, incluidas las de lectura.")

        # Reglas explícitas sobre herramienta
        for s in self.scopes:
            if s.pattern.startswith("tool:") and fnmatch.fnmatch(
                    f"tool:{action}", s.pattern):
                if s.decision == Decision.DENY:
                    return Decision.DENY, f"herramienta bloqueada: {s.note or s.pattern}"
                if principal.trust < s.min_trust:
                    return Decision.CONFIRM, f"requiere confianza {s.min_trust.name}"
                return s.decision, s.note or "regla de herramienta"

        if target:
            expanded = str(Path(target).expanduser()) if target.startswith("~") else target
            for s in self.scopes:
                if s.pattern.startswith("tool:"):
                    continue
                if s.matches(target) or s.matches(expanded):
                    if s.decision == Decision.DENY:
                        return Decision.DENY, f"ámbito prohibido: {s.note or s.pattern}"
                    if principal.trust < s.min_trust:
                        return Decision.CONFIRM, (
                            f"'{s.pattern}' requiere confianza "
                            f"{s.min_trust.name} y tienes {principal.trust.name}")
                    if s.decision == Decision.ALLOW:
                        # Autonomía real: dentro del ámbito no se pregunta nada.
                        return Decision.ALLOW, f"ámbito libre: {s.pattern}"
                    return Decision.CONFIRM, s.note or f"ámbito vigilado: {s.pattern}"

        # Fuera de todo ámbito declarado: confirmar. No prohibir —el agente
        # puede trabajar—, pero tampoco asumir permiso que nadie dio.
        if mutating:
            return Decision.CONFIRM, (
                f"'{target or action}' está fuera de tus ámbitos declarados. "
                "Añádelo con `fib scope add` si quieres que opere libre ahí.")
        return Decision.ALLOW, "lectura fuera de ámbito"

    # ------------------------------------------------------------------

    def add_scope(self, pattern: str, decision: Decision,
                  min_trust: Trust = Trust.MEMBER, note: str = "") -> None:
        self.scopes = list(CORE_DENY) + [
            Scope(pattern, decision, min_trust, note)
        ] + [s for s in self.scopes if s not in CORE_DENY]
        self.save()

    def describe(self) -> list[str]:
        icons = {Decision.ALLOW: "libre  ", Decision.CONFIRM: "confirma",
                 Decision.DENY: "bloquea"}
        return [f"{icons[s.decision]}  {s.pattern}"
                + (f"  ({s.note})" if s.note else "") for s in self.scopes]


MUTATING_PREFIXES = ("file.write", "file.delete", "file.move", "shell", "ssh.",
                     "remote.", "screen.click", "screen.type", "screen.key",
                     "screen.drag", "mcp.", "agent.spawn")


def _is_mutating(action: str) -> bool:
    return any(action.startswith(p) for p in MUTATING_PREFIXES)
