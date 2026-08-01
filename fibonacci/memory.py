"""
FIBONACCI — Memoria.

Dos mejoras concretas sobre el modelo de Hermes.

**1. Decaimiento y contradicción.** Hermes acumula memoria curada por el agente
y construye un modelo de quién eres a lo largo de las sesiones. El problema es
que todo pesa igual para siempre: "trabaja en Acme" de hace un año convive en
silencio con "empezó en Beta" de la semana pasada, y el agente elige al azar.
Aquí cada nota decae con vida media configurable, y al insertar una nota que
contradice a otra el sistema **la marca en vez de dejarlas coexistir**.

**2. Skills que se ganan la activación.** Hermes crea skills tras tareas
complejas y las mejora durante el uso. Es su mejor idea y también su mayor
riesgo: una skill mala degrada en silencio todos los runs futuros y nada lo
señala. Fibonacci las promueve por etapas —candidata → sombra → activa— con
tasa de éxito medida. Una skill que empeora las cosas se retira sola.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .contracts import Note, Skill, Turn
from .platform import data_dir
from .store import Store

log = logging.getLogger("fibonacci.memory")

MIGRATIONS = [
    (1, "esquema inicial", """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
            source TEXT, confidence REAL, half_life REAL, supersedes TEXT,
            embedding TEXT, ts REAL NOT NULL, retired INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind, retired);

        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
            description TEXT, triggers TEXT, status TEXT NOT NULL,
            trials INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
            version TEXT, source TEXT, ts REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ts REAL NOT NULL,
            user TEXT, assistant TEXT, tools TEXT, tokens INTEGER, surface TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, ts);

        CREATE TABLE IF NOT EXISTS conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_a TEXT, note_b TEXT, ts REAL, resolved INTEGER DEFAULT 0
        );
    """),
]

# FTS5 puede no estar compilado (Termux, builds minimos). Se intenta aparte
# de las migraciones para que su ausencia no bloquee el arranque.
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    user, assistant, content=turns, content_rowid=rowid
);
"""


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


class Memory:
    def __init__(self, path: str | Path | None = None,
                 embedder: Callable[[list[str]], list[list[float]]] | None = None):
        self.path = Path(path) if path else data_dir() / "memory.db"
        self.store = Store(self.path, MIGRATIONS)
        try:
            self.store.db.executescript(FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False       # degrada a busqueda lexica, no falla
            log.info("FTS5 no disponible; busqueda lexica en su lugar")
        self.embedder = embedder

    @property
    def db(self):
        """Conexion del hilo actual. Ver store.py."""
        return self.store.db

    # -- Notas -----------------------------------------------------------

    def remember(self, note: Note, detect_conflicts: bool = True) -> tuple[str, list[Note]]:
        """Devuelve (id, contradicciones detectadas)."""
        conflicts: list[Note] = []
        if detect_conflicts:
            conflicts = self._find_conflicts(note)
            for c in conflicts:
                self.db.execute(
                    "INSERT INTO conflicts(note_a, note_b, ts) VALUES (?,?,?)",
                    (note.id, c.id, time.time()),
                )

        if self.embedder and not note.embedding:
            try:
                note.embedding = self.embedder([note.content])[0]
            except Exception:  # noqa: BLE001
                pass

        self.db.execute(
            "INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?,?,?,0)",
            (note.id, note.content, note.kind, note.source, note.confidence,
             note.half_life_days, note.supersedes,
             json.dumps(note.embedding) if note.embedding else None, note.ts),
        )
        if note.supersedes:
            self.db.execute("UPDATE notes SET retired=1 WHERE id=?", (note.supersedes,))
        return note.id, conflicts

    def _find_conflicts(self, note: Note) -> list[Note]:
        """
        Heurística deliberadamente conservadora: misma clase y alto solapamiento
        léxico, pero con diferencias sustantivas. Prefiere no marcar a marcar de
        más — una alerta de contradicción falsa entrena al usuario a ignorarlas.
        """
        out = []
        new_t = _tokens(note.content)
        if len(new_t) < 3:
            return out
        for cand in self.recall_all(kind=note.kind):
            if cand.id == note.id or cand.stale:
                continue
            old_t = _tokens(cand.content)
            overlap = len(new_t & old_t) / max(len(new_t | old_t), 1)
            if 0.35 < overlap < 0.85 and new_t != old_t:
                out.append(cand)
        return out[:3]

    def recall(self, query: str, k: int = 6, kind: str | None = None) -> list[Note]:
        notes = self.recall_all(kind=kind)
        if not notes:
            return []
        qvec = None
        if self.embedder:
            try:
                qvec = self.embedder([query])[0]
            except Exception:  # noqa: BLE001
                pass
        qt = _tokens(query)
        scored = []
        for n in notes:
            if qvec and n.embedding:
                sim = _cosine(qvec, n.embedding)
            else:
                nt = _tokens(n.content)
                sim = len(qt & nt) / max(len(qt), 1)
            # La confianza decaída pondera: lo viejo compite en desventaja.
            scored.append((sim * 0.65 + n.current_confidence() * 0.35, n))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for s, n in scored[:k] if s > 0.1]

    def recall_all(self, kind: str | None = None, include_stale: bool = False) -> list[Note]:
        sql, params = "SELECT * FROM notes WHERE retired=0", []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        notes = [_row_to_note(r) for r in self.db.execute(sql, params).fetchall()]
        return notes if include_stale else [n for n in notes if not n.stale]

    def open_conflicts(self) -> list[tuple[Note, Note]]:
        rows = self.db.execute(
            "SELECT * FROM conflicts WHERE resolved=0 ORDER BY ts DESC LIMIT 20"
        ).fetchall()
        out = []
        for r in rows:
            a = self.get_note(r["note_a"])
            b = self.get_note(r["note_b"])
            if a and b:
                out.append((a, b))
        return out

    def resolve_conflict(self, keep_id: str, drop_id: str) -> None:
        self.db.execute("UPDATE notes SET retired=1 WHERE id=?", (drop_id,))
        self.db.execute(
            "UPDATE conflicts SET resolved=1 WHERE note_a IN (?,?) AND note_b IN (?,?)",
            (keep_id, drop_id, keep_id, drop_id),
        )

    def get_note(self, note_id: str) -> Note | None:
        r = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return _row_to_note(r) if r else None

    def forget_stale(self) -> int:
        """Higiene: lo que decayó bajo el umbral se retira. Un agente que
        recuerda todo para siempre termina recordando puras cosas falsas."""
        n = 0
        for note in self.recall_all(include_stale=True):
            if note.stale:
                self.db.execute("UPDATE notes SET retired=1 WHERE id=?", (note.id,))
                n += 1
        return n

    # -- Skills con promoción por evidencia --------------------------------

    def save_skill(self, skill: Skill) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (skill.id, skill.name, skill.body, skill.description,
             json.dumps(skill.triggers), skill.status, skill.trials, skill.wins,
             skill.version, skill.source, time.time()),
        )

    def skills(self, status: str | None = None) -> list[Skill]:
        sql, params = "SELECT * FROM skills", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        return [_row_to_skill(r) for r in self.db.execute(sql, params).fetchall()]

    def select_skills(self, text: str, k: int = 3) -> list[Skill]:
        """Solo activas y en sombra. Las candidatas nunca tocan un prompt real."""
        low = text.lower()
        scored = []
        for s in self.skills():
            if s.status not in ("active", "shadow"):
                continue
            hits = sum(1 for t in s.triggers if t.lower() in low)
            if hits:
                scored.append((hits + s.win_rate, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:k]]

    def score_skill(self, name: str, won: bool) -> str:
        """
        Ciclo de vida por evidencia:
          candidata --(3 pruebas)--> sombra --(8 pruebas, ≥70%)--> activa
          activa --(≥6 pruebas, <40%)--> retirada
        """
        r = self.db.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        if r is None:
            return "desconocida"
        s = _row_to_skill(r)
        s.trials += 1
        s.wins += int(won)

        if s.status == "candidate" and s.trials >= 3:
            s.status = "shadow"
        elif s.status == "shadow" and s.trials >= 8:
            s.status = "active" if s.win_rate >= 0.70 else "retired"
        elif s.status == "active" and s.trials >= 6 and s.win_rate < 0.40:
            s.status = "retired"
            log.warning("Skill retirada por bajo desempeño: %s (%.0f%%)",
                        name, s.win_rate * 100)

        self.save_skill(s)
        return s.status

    # -- Sesiones ----------------------------------------------------------

    def add_turn(self, turn: Turn) -> None:
        self.db.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
            (turn.id, turn.session_id, turn.ts, turn.user, turn.assistant,
             json.dumps(turn.tools_used), turn.tokens, turn.surface),
        )
        if self.fts:
            try:
                self.db.execute(
                    "INSERT INTO turns_fts(rowid, user, assistant) "
                    "VALUES ((SELECT rowid FROM turns WHERE id=?),?,?)",
                    (turn.id, turn.user, turn.assistant),
                )
            except sqlite3.OperationalError:
                pass

    def session(self, session_id: str, limit: int = 100) -> list[Turn]:
        rows = self.db.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [_row_to_turn(r) for r in reversed(rows)]

    def search_turns(self, query: str, limit: int = 10) -> list[Turn]:
        if self.fts:
            try:
                rows = self.db.execute(
                    "SELECT t.* FROM turns t JOIN turns_fts f ON t.rowid=f.rowid "
                    "WHERE turns_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
                if rows:
                    return [_row_to_turn(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        rows = self.db.execute(
            "SELECT * FROM turns WHERE user LIKE ? OR assistant LIKE ? "
            "ORDER BY ts DESC LIMIT ?", (like, like, limit),
        ).fetchall()
        return [_row_to_turn(r) for r in rows]

    def stats(self) -> dict:
        q = lambda s, p=(): self.db.execute(s, p).fetchone()[0]  # noqa: E731
        return {
            "notas": q("SELECT COUNT(*) FROM notes WHERE retired=0"),
            "contradicciones_abiertas": q("SELECT COUNT(*) FROM conflicts WHERE resolved=0"),
            "skills_activas": q("SELECT COUNT(*) FROM skills WHERE status='active'"),
            "skills_en_prueba": q("SELECT COUNT(*) FROM skills WHERE status IN "
                                  "('candidate','shadow')"),
            "turnos": q("SELECT COUNT(*) FROM turns"),
            "sesiones": q("SELECT COUNT(DISTINCT session_id) FROM turns"),
        }


def _row_to_note(r) -> Note:
    return Note(
        id=r["id"], content=r["content"], kind=r["kind"], source=r["source"] or "",
        confidence=r["confidence"], half_life_days=r["half_life"],
        supersedes=r["supersedes"],
        embedding=json.loads(r["embedding"]) if r["embedding"] else [],
        ts=r["ts"],
    )


def _row_to_skill(r) -> Skill:
    return Skill(
        id=r["id"], name=r["name"], body=r["body"], description=r["description"] or "",
        triggers=json.loads(r["triggers"] or "[]"), status=r["status"],
        trials=r["trials"], wins=r["wins"], version=r["version"] or "0.1",
        source=r["source"] or "learned",
    )


def _row_to_turn(r) -> Turn:
    return Turn(
        id=r["id"], session_id=r["session_id"], ts=r["ts"], user=r["user"] or "",
        assistant=r["assistant"] or "", tools_used=json.loads(r["tools"] or "[]"),
        tokens=r["tokens"] or 0, surface=r["surface"] or "cli",
    )
