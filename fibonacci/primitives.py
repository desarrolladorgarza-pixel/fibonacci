"""
FIBONACCI — Primitivas agénticas.

Hasta ahora, todo lo que hacía Fibonacci de robusto estaba cableado dentro del
bucle de `chat`: el reintento, el presupuesto, la verificación. Eso funciona
pero no se puede componer — si quieres "intenta esto tres veces, y si falla
prueba la alternativa, y verifica antes de darlo por bueno", tienes que
escribirlo a mano cada vez.

Este módulo extrae esos patrones como **primitivas componibles**: un álgebra
pequeña que se usa igual desde código Python y desde el modelo (se exponen como
herramientas). La idea es que la robustez sea una propiedad que se declara, no
un accidente de cómo salió el prompt.

    plan = (Retry(3) >> Verify(criterio) >> Checkpoint("antes-del-deploy"))

Las ocho primitivas:

  Retry       reintenta con retroceso exponencial
  Fallback    prueba alternativas en orden hasta que una funcione
  Verify      comprueba el resultado antes de darlo por bueno
  Checkpoint  marca un punto al que se puede volver (usa el journal)
  Race        lanza varias en paralelo y se queda con la primera que sirva
  Budget      acota gasto/tiempo de un bloque
  Gate        exige una condición antes de continuar
  Observe     espera a que se cumpla algo, con timeout

## Por qué esto importa más de lo que parece

Un agente sin estas primitivas resuelve los fallos improvisando, y su
improvisación varía con el modelo, la temperatura y el día. Con ellas, el
comportamiento ante el fallo es **determinista y auditable**: sabes cuántas
veces reintentó, con qué alternativas, y qué verificó. Eso es lo que separa un
agente de una demo.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("fibonacci.primitives")


@dataclass
class Outcome:
    """Resultado de una primitiva. Siempre lleva su historia."""

    ok: bool
    value: Any = None
    error: str = ""
    attempts: int = 0
    elapsed_ms: int = 0
    trace: list[str] = field(default_factory=list)

    def note(self, msg: str) -> "Outcome":
        self.trace.append(msg)
        return self

    def __bool__(self) -> bool:
        return self.ok


class Primitive:
    """Base componible. `a >> b` encadena: b recibe el valor de a."""

    name = "primitive"

    def run(self, fn: Callable[[], Any], ctx: dict | None = None) -> Outcome:
        raise NotImplementedError

    def __rshift__(self, other: "Primitive") -> "Chain":
        return Chain([self, other])


class Chain(Primitive):
    name = "chain"

    def __init__(self, parts: list[Primitive]):
        self.parts = parts

    def __rshift__(self, other: Primitive) -> "Chain":
        return Chain(self.parts + [other])

    def run(self, fn: Callable[[], Any], ctx: dict | None = None) -> Outcome:
        ctx = ctx if ctx is not None else {}
        t0 = time.time()
        actual = fn
        resultado = Outcome(True)
        historia: list[str] = []
        intentos = 0

        for p in self.parts:
            resultado = p.run(actual, ctx)
            intentos += resultado.attempts
            # La traza se acumula: si el paso 3 falla, quieres ver qué hicieron
            # el 1 y el 2. Cada primitiva devuelve un Outcome nuevo, así que
            # hay que arrastrar la historia a mano.
            historia.extend(resultado.trace)
            historia.append(f"{p.name}: {'ok' if resultado.ok else resultado.error[:80]}")
            resultado.trace = list(historia)
            resultado.attempts = intentos
            if not resultado.ok:
                resultado.elapsed_ms = int((time.time() - t0) * 1000)
                return resultado
            valor = resultado.value

            def actual(v=valor):        # el siguiente recibe lo producido
                return v

        resultado.elapsed_ms = int((time.time() - t0) * 1000)
        return resultado


# ---------------------------------------------------------------------------

class Retry(Primitive):
    """
    Reintenta con retroceso exponencial y jitter.

    El jitter no es adorno: sin él, N agentes que fallan a la vez reintentan a
    la vez y vuelven a tumbar el servicio que se estaba recuperando.
    """

    name = "retry"

    def __init__(self, attempts: int = 3, base_delay: float = 1.0,
                 max_delay: float = 30.0,
                 retry_on: Callable[[Exception], bool] | None = None):
        self.attempts = max(1, attempts)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = retry_on or (lambda e: True)

    def run(self, fn, ctx=None) -> Outcome:
        import random

        t0 = time.time()
        ultimo = ""
        for intento in range(1, self.attempts + 1):
            try:
                v = fn()
                return Outcome(True, v, attempts=intento,
                               elapsed_ms=int((time.time() - t0) * 1000))
            except Exception as exc:  # noqa: BLE001
                ultimo = f"{type(exc).__name__}: {exc}"
                if not self.retry_on(exc) or intento == self.attempts:
                    break
                espera = min(self.base_delay * (2 ** (intento - 1)), self.max_delay)
                espera *= 0.5 + random.random()          # jitter
                log.info("Reintento %d/%d en %.1fs: %s",
                         intento, self.attempts, espera, ultimo[:80])
                time.sleep(espera)
        return Outcome(False, error=ultimo, attempts=self.attempts,
                       elapsed_ms=int((time.time() - t0) * 1000))


class Fallback(Primitive):
    """
    Alternativas en orden. La primera que funcione gana.

    Distinto de Retry: Retry repite *lo mismo* esperando que el mundo cambie;
    Fallback prueba *otra cosa* porque la primera no va a funcionar nunca.
    Confundirlos produce agentes que reintentan 5 veces algo imposible.
    """

    name = "fallback"

    def __init__(self, alternatives: list[Callable[[], Any]] | None = None):
        self.alternatives = alternatives or []

    def run(self, fn, ctx=None) -> Outcome:
        t0 = time.time()
        opciones = [fn] + list(self.alternatives)
        errores = []
        for i, opcion in enumerate(opciones):
            try:
                v = opcion()
                return Outcome(True, v, attempts=i + 1,
                               elapsed_ms=int((time.time() - t0) * 1000)
                               ).note(f"alternativa {i} funcionó")
            except Exception as exc:  # noqa: BLE001
                errores.append(f"[{i}] {type(exc).__name__}: {exc}")
        return Outcome(False, error=" | ".join(errores), attempts=len(opciones),
                       elapsed_ms=int((time.time() - t0) * 1000))


class Verify(Primitive):
    """
    Comprueba el resultado antes de darlo por bueno. Si el verificador falla,
    el resultado se descarta aunque la operación haya "salido bien".

    Un agente que declara éxito porque no hubo excepción es un agente que
    miente sin querer.
    """

    name = "verify"

    def __init__(self, check: Callable[[Any], bool], description: str = "",
                 repair: Callable[[Any], Any] | None = None):
        self.check = check
        self.description = description or "verificación"
        self.repair = repair

    def run(self, fn, ctx=None) -> Outcome:
        t0 = time.time()
        try:
            v = fn()
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"{type(exc).__name__}: {exc}",
                           elapsed_ms=int((time.time() - t0) * 1000))

        try:
            if self.check(v):
                return Outcome(True, v, elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"el verificador falló: {exc}")

        if self.repair:
            try:
                reparado = self.repair(v)
                if self.check(reparado):
                    return Outcome(True, reparado,
                                   elapsed_ms=int((time.time() - t0) * 1000)
                                   ).note("reparado tras fallar la verificación")
            except Exception as exc:  # noqa: BLE001
                return Outcome(False, error=f"la reparación falló: {exc}")

        return Outcome(False, value=v,
                       error=f"no pasó {self.description}",
                       elapsed_ms=int((time.time() - t0) * 1000))


class Checkpoint(Primitive):
    """
    Marca un punto de retorno usando el journal. Si lo que viene después falla,
    `rollback()` revierte hasta aquí.

    Es la primitiva que conecta el álgebra con la garantía central del producto:
    la reversibilidad deja de ser algo que haces a mano y pasa a ser algo que
    declaras en el plan.
    """

    name = "checkpoint"

    def __init__(self, label: str, journal=None, session: str = "default"):
        self.label = label
        self.journal = journal
        self.session = session
        self.marker: float = 0.0

    def run(self, fn, ctx=None) -> Outcome:
        self.marker = time.time()
        if ctx is not None:
            ctx.setdefault("checkpoints", {})[self.label] = self.marker
        if self.journal:
            self.journal.trace(self.session, "checkpoint", self.label)
        try:
            v = fn()
            return Outcome(True, v).note(f"punto '{self.label}' establecido")
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"{type(exc).__name__}: {exc}")

    def rollback(self, force: bool = False) -> tuple[int, list[str]]:
        """Revierte todo lo que ocurrió después del punto."""
        if not self.journal:
            return 0, ["sin journal: no hay nada que revertir"]
        from .contracts import ActionStatus

        filas = self.journal.store.all(
            "SELECT * FROM actions WHERE session_id=? AND ts>=? AND status=? "
            "ORDER BY ts DESC",
            (self.session, self.marker, ActionStatus.APPLIED.value))
        hechas, notas = 0, []
        for r in filas:
            from .journal import _row_to_action
            ok, msg = self.journal._undo(_row_to_action(r), force=force)
            notas.append(msg)
            hechas += ok
        return hechas, notas


class Race(Primitive):
    """
    Varias estrategias en paralelo; gana la primera que produzca un resultado
    válido. Las demás se abandonan.

    Útil cuando no sabes cuál servirá y esperar en serie cuesta más que
    ejecutar de más: consultar tres fuentes, probar dos modelos.
    """

    name = "race"

    def __init__(self, competitors: list[Callable[[], Any]],
                 accept: Callable[[Any], bool] | None = None,
                 timeout: float = 120.0):
        self.competitors = competitors
        self.accept = accept or (lambda v: v is not None)
        self.timeout = timeout

    def run(self, fn, ctx=None) -> Outcome:
        t0 = time.time()
        candidatos = ([fn] if fn else []) + list(self.competitors)
        if not candidatos:
            return Outcome(False, error="sin competidores")

        with ThreadPoolExecutor(max_workers=len(candidatos)) as pool:
            futuros = {pool.submit(c): i for i, c in enumerate(candidatos)}
            pendientes = set(futuros)
            errores = []
            while pendientes:
                restante = self.timeout - (time.time() - t0)
                if restante <= 0:
                    break
                listos, pendientes = wait(pendientes, timeout=restante,
                                          return_when=FIRST_COMPLETED)
                for f in listos:
                    try:
                        v = f.result()
                        if self.accept(v):
                            for p in pendientes:
                                p.cancel()
                            return Outcome(
                                True, v, attempts=futuros[f] + 1,
                                elapsed_ms=int((time.time() - t0) * 1000)
                            ).note(f"ganó el competidor {futuros[f]}")
                    except Exception as exc:  # noqa: BLE001
                        errores.append(f"[{futuros[f]}] {exc}")
        return Outcome(False, error=" | ".join(errores) or "nadie produjo un resultado válido",
                       elapsed_ms=int((time.time() - t0) * 1000))


class Budget(Primitive):
    """Acota un bloque en tiempo y coste. Corta a media obra si se pasa."""

    name = "budget"

    def __init__(self, max_seconds: float = 300.0, max_usd: float = 1.0,
                 meter: Callable[[], float] | None = None):
        self.max_seconds = max_seconds
        self.max_usd = max_usd
        self.meter = meter

    def run(self, fn, ctx=None) -> Outcome:
        t0 = time.time()
        gasto_inicial = self.meter() if self.meter else 0.0
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(fn)
            try:
                v = fut.result(timeout=self.max_seconds)
            except TimeoutError:
                return Outcome(False, error=f"excedió {self.max_seconds:g}s",
                               elapsed_ms=int((time.time() - t0) * 1000))
            except Exception as exc:  # noqa: BLE001
                return Outcome(False, error=f"{type(exc).__name__}: {exc}")

        if self.meter:
            gastado = self.meter() - gasto_inicial
            if gastado > self.max_usd:
                return Outcome(False, value=v,
                               error=f"gastó ${gastado:.4f} (límite ${self.max_usd:.2f})")
        return Outcome(True, v, elapsed_ms=int((time.time() - t0) * 1000))


class Gate(Primitive):
    """
    Exige una condición antes de seguir. A diferencia de Verify —que comprueba
    el resultado— Gate comprueba la *precondición*: no ejecuta si no se cumple.
    """

    name = "gate"

    def __init__(self, condition: Callable[[], bool], description: str = "",
                 on_blocked: Callable[[], Any] | None = None):
        self.condition = condition
        self.description = description or "precondición"
        self.on_blocked = on_blocked

    def run(self, fn, ctx=None) -> Outcome:
        try:
            permitido = self.condition()
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"la condición falló al evaluarse: {exc}")

        if not permitido:
            if self.on_blocked:
                try:
                    return Outcome(True, self.on_blocked()).note(
                        f"bloqueado por {self.description}; se usó la salida alterna")
                except Exception as exc:  # noqa: BLE001
                    return Outcome(False, error=str(exc))
            return Outcome(False, error=f"bloqueado: no se cumple {self.description}")

        try:
            return Outcome(True, fn())
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"{type(exc).__name__}: {exc}")


class Observe(Primitive):
    """
    Espera a que se cumpla una condición, sondeando con intervalo.

    Para trabajo asíncrono del mundo real: un despliegue que tarda, un archivo
    que aparece, un job que termina. Sin esto, el agente hace `sleep` a ojo.
    """

    name = "observe"

    def __init__(self, condition: Callable[[], bool], timeout: float = 300.0,
                 interval: float = 5.0, description: str = ""):
        self.condition = condition
        self.timeout = timeout
        self.interval = interval
        self.description = description or "la condición"

    def run(self, fn, ctx=None) -> Outcome:
        t0 = time.time()
        try:
            valor = fn() if fn else None
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, error=f"{type(exc).__name__}: {exc}")

        sondeos = 0
        while time.time() - t0 < self.timeout:
            sondeos += 1
            try:
                if self.condition():
                    return Outcome(True, valor, attempts=sondeos,
                                   elapsed_ms=int((time.time() - t0) * 1000)
                                   ).note(f"{self.description} se cumplió")
            except Exception as exc:  # noqa: BLE001
                log.debug("sondeo falló: %s", exc)
            time.sleep(self.interval)

        return Outcome(False, value=valor, attempts=sondeos,
                       error=f"{self.description} no se cumplió en {self.timeout:g}s",
                       elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------------------
# Recetas: combinaciones que resuelven casos comunes
# ---------------------------------------------------------------------------

def robust(attempts: int = 3, check: Callable[[Any], bool] | None = None) -> Primitive:
    """Reintento + verificación. El patrón por defecto para casi todo."""
    if check is None:
        return Retry(attempts)
    return Chain([Retry(attempts), Verify(check)])


def transactional(label: str, journal, session: str,
                  check: Callable[[Any], bool] | None = None) -> Chain:
    """
    Punto de retorno + verificación. Si la verificación falla, tienes el
    checkpoint para revertir todo lo hecho desde entonces.

        cp = Checkpoint("antes-migracion", journal, "s1")
        r = (cp >> Verify(migro_bien)).run(hacer_migracion)
        if not r.ok:
            cp.rollback()
    """
    cp = Checkpoint(label, journal, session)
    partes: list[Primitive] = [cp]
    if check:
        partes.append(Verify(check))
    chain = Chain(partes)
    chain.checkpoint = cp          # type: ignore[attr-defined]
    return chain


def resilient(alternatives: list[Callable[[], Any]],
              attempts: int = 2) -> Primitive:
    """
    Reintenta cada alternativa antes de pasar a la siguiente.

    Ojo con la composición: `Retry >> Fallback` NO hace esto. La cadena corta
    al primer fallo, así que si el Retry se agota el Fallback nunca corre. Lo
    correcto es envolver cada alternativa en su propio Retry — es un Fallback
    de Retries, no un Retry seguido de Fallback.
    """

    class _Resilient(Primitive):
        name = "resilient"

        def run(self, fn, ctx=None) -> Outcome:
            t0 = time.time()
            opciones = [fn] + list(alternatives)
            errores, total = [], 0
            for i, opcion in enumerate(opciones):
                r = Retry(attempts, base_delay=0.5).run(opcion, ctx)
                total += r.attempts
                if r.ok:
                    return Outcome(True, r.value, attempts=total,
                                   elapsed_ms=int((time.time() - t0) * 1000)
                                   ).note(f"alternativa {i} funcionó tras "
                                          f"{r.attempts} intento(s)")
                errores.append(f"[{i}] {r.error}")
            return Outcome(False, error=" | ".join(errores), attempts=total,
                           elapsed_ms=int((time.time() - t0) * 1000))

    return _Resilient()
