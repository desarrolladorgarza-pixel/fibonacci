"""
FIBONACCI — Almacenamiento.

Corrige dos fallos estructurales de la v0.1.0:

**Concurrencia.** Antes cada componente hacía `sqlite3.connect(...,
check_same_thread=False)` y compartía una conexión entre hilos. Eso produce
`database is locked` en cuanto corren el CLI y el servidor MCP a la vez —el
caso de uso que la propia documentación recomienda— y, peor, corrupción
latente porque una conexión SQLite no es segura entre hilos aunque se lo
digas. Aquí: WAL, `busy_timeout`, y **una conexión por hilo**.

**Migraciones.** Antes todo era `CREATE TABLE IF NOT EXISTS`, así que cualquier
cambio de columna en v0.2 rompía las instalaciones existentes en silencio.
Ahora `PRAGMA user_version` con migraciones ordenadas y verificables.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger("fibonacci.store")

Migration = tuple[int, str, str]     # (versión, nombre, SQL)


class Store:
    """Base SQLite segura entre hilos y versionada."""

    def __init__(self, path: str | Path, migrations: Sequence[Migration]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrations = sorted(migrations, key=lambda m: m[0])
        self._write_lock = threading.RLock()
        self._migrate()

    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL permite un escritor y varios lectores en paralelo. Sin esto,
        # cualquier lectura bloquea al agente mientras escribe.
        conn.execute("PRAGMA journal_mode=WAL")
        # Si otro proceso tiene el lock, espera en vez de fallar de inmediato.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL con WAL es seguro ante caídas del proceso (no ante corte de
        # energía). Para un agente personal es el punto correcto.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @property
    def db(self) -> sqlite3.Connection:
        """Conexión propia del hilo actual. Nunca compartida."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        conn = self.db
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        target = self._migrations[-1][0] if self._migrations else 0

        if current > target:
            raise RuntimeError(
                f"{self.path.name}: la base está en versión {current} pero este "
                f"Fibonacci solo conoce hasta {target}. Actualiza Fibonacci; no "
                "voy a degradar tu base."
            )

        for version, name, sql in self._migrations:
            if version <= current:
                continue
            with self._write_lock:
                try:
                    # `executescript` hace COMMIT implícito de cualquier
                    # transacción pendiente antes de correr, así que un
                    # BEGIN externo se pierde. La transacción va dentro.
                    conn.executescript(
                        f"BEGIN;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
                    )
                    log.info("%s: migración %d aplicada (%s)", self.path.name, version, name)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    log.error("%s: falló la migración %d (%s)", self.path.name, version, name)
                    raise

    @property
    def version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def write(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        """Escritura serializada en el proceso. WAL cubre entre procesos."""
        with self._write_lock:
            return self.db.execute(sql, params)

    def transaction(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        """Varias escrituras como una unidad. O todas, o ninguna."""
        with self._write_lock:
            conn = self.db
            try:
                conn.execute("BEGIN")
                fn(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def one(self, sql: str, params: Sequence = ()):
        return self.db.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence = (), default=0):
        r = self.one(sql, params)
        return r[0] if r is not None and r[0] is not None else default

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def has_fts5(self) -> bool:
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe "
                            "USING fts5(x)")
            self.db.execute("DROP TABLE IF EXISTS _fts_probe")
            return True
        except sqlite3.OperationalError:
            return False
