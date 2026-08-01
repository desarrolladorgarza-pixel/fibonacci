"""
FIBONACCI — Tareas durables.

Hermes vive donde tú vives: Telegram, Discord, CLI, todo desde un gateway. Es
buena idea, pero la *conversación* es lo portátil; el *trabajo* sigue atado al
proceso que lo arrancó. Si el proceso muere a mitad de una tarea larga, se
perdió.

Aquí el trabajo es un objeto persistido, no un hilo en memoria. Arrancas una
tarea en la laptop, la cierras, y desde el teléfono ves en qué paso va y la
reanudas. La continuidad es del trabajo, no solo del chat.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .contracts import DurableTask, Step, TaskState
from .platform import data_dir
from .store import Store

MIGRATIONS = [
    (1, "esquema inicial", """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, session_id TEXT NOT NULL,
            steps TEXT NOT NULL, state TEXT NOT NULL, cursor INTEGER DEFAULT 0,
            surface TEXT, result TEXT, created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, updated_at);
    """),
]


class TaskStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "tasks.db"
        self.store = Store(self.path, MIGRATIONS)

    @property
    def db(self):
        return self.store.db

    def save(self, t: DurableTask) -> None:
        t.updated_at = time.time()
        self.store.write(
            "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t.id, t.goal, t.session_id,
             json.dumps([{"id": s.id, "description": s.description,
                          "state": s.state.value, "output": s.output}
                         for s in t.steps]),
             t.state.value, t.cursor, t.surface, t.result, t.created_at, t.updated_at))

    def get(self, task_id: str) -> DurableTask | None:
        r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row(r) if r else None

    def list(self, state: TaskState | None = None, limit: int = 20) -> list[DurableTask]:
        sql, params = "SELECT * FROM tasks", []
        if state:
            sql += " WHERE state=?"
            params.append(state.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [_row(r) for r in self.db.execute(sql, params).fetchall()]

    def resumable(self) -> list[DurableTask]:
        """Lo que quedó a medias: se reanuda desde cualquier dispositivo."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE state IN (?,?,?) ORDER BY updated_at DESC",
            (TaskState.RUNNING.value, TaskState.QUEUED.value,
             TaskState.WAITING_HUMAN.value)).fetchall()
        return [_row(r) for r in rows]

    def cancel(self, task_id: str) -> bool:
        cur = self.store.write("UPDATE tasks SET state=? WHERE id=?",
                              (TaskState.CANCELLED.value, task_id))
        return cur.rowcount > 0


def _row(r: sqlite3.Row) -> DurableTask:
    steps = [Step(id=s["id"], description=s["description"],
                  state=TaskState(s["state"]), output=s.get("output", ""))
             for s in json.loads(r["steps"])]
    return DurableTask(
        id=r["id"], goal=r["goal"], session_id=r["session_id"], steps=steps,
        state=TaskState(r["state"]), cursor=r["cursor"],
        surface=r["surface"] or "cli", result=r["result"] or "",
        created_at=r["created_at"], updated_at=r["updated_at"])
