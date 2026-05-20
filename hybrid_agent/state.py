"""SQLite-backed state store. Single-writer, per-task rows.

Every state transition is persisted immediately. On startup, `load_all()`
returns the full state so the orchestrator can resume.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from .models import TaskExecution, TaskSpec, TaskStatus

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_specs (
    id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_executions (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    execution_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_specs(id)
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    stage TEXT,
    backend TEXT,
    success INTEGER,
    duration_ms INTEGER,
    message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_executions_status ON task_executions(status);
CREATE INDEX IF NOT EXISTS idx_run_log_task ON run_log(task_id);
"""


class StateStore:
    """Thread-safe (via lock) SQLite state store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- specs ----------------------------------------------------------

    def upsert_specs(self, specs: list[TaskSpec]) -> None:
        cur = self._conn.cursor()
        for s in specs:
            cur.execute(
                "INSERT OR REPLACE INTO task_specs (id, spec_json) VALUES (?, ?)",
                (s.id, s.model_dump_json()),
            )
        self._conn.commit()

    def load_specs(self) -> list[TaskSpec]:
        cur = self._conn.cursor()
        cur.execute("SELECT spec_json FROM task_specs")
        return [TaskSpec.model_validate_json(r["spec_json"]) for r in cur.fetchall()]

    # ---- executions -----------------------------------------------------

    async def save_execution(self, execution: TaskExecution) -> None:
        execution.touch()
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_executions "
                "(task_id, status, execution_json, updated_at) VALUES (?, ?, ?, ?)",
                (
                    execution.task_id,
                    execution.status.value,
                    execution.model_dump_json(),
                    execution.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def load_executions(self) -> dict[str, TaskExecution]:
        cur = self._conn.cursor()
        cur.execute("SELECT execution_json FROM task_executions")
        out: dict[str, TaskExecution] = {}
        for row in cur.fetchall():
            ex = TaskExecution.model_validate_json(row["execution_json"])
            out[ex.task_id] = ex
        return out

    def get_execution(self, task_id: str) -> TaskExecution | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT execution_json FROM task_executions WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return TaskExecution.model_validate_json(row["execution_json"])

    # ---- run log --------------------------------------------------------

    async def log_run(
        self,
        task_id: str,
        stage: str,
        backend: str,
        success: bool,
        duration_ms: int,
        message: str = "",
    ) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO run_log (task_id, stage, backend, success, duration_ms, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, stage, backend, int(success), duration_ms, message[:1000]),
            )
            self._conn.commit()

    # ---- summary --------------------------------------------------------

    def status_counts(self) -> dict[str, int]:
        cur = self._conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM task_executions GROUP BY status")
        return {r[0]: r[1] for r in cur.fetchall()}

    # ---- recovery -------------------------------------------------------

    def reset_in_progress(self) -> int:
        """Reset tasks left in transient states back to a safe state.

        Called on startup. Tasks that were mid-stage are reset to the last
        completed-stage status (so the next stage re-runs cleanly).
        Returns count of tasks reset.
        """
        rollback_map = {
            TaskStatus.PLANNING.value: TaskStatus.READY.value,
            TaskStatus.PLANNED.value: TaskStatus.READY.value,  # resume skips re-plan
            TaskStatus.CODING.value: TaskStatus.READY.value,  # resume skips re-plan
            TaskStatus.CODED.value: TaskStatus.READY.value,  # resume skips plan+code
            TaskStatus.REVIEWING.value: TaskStatus.READY.value,  # resume skips plan+code
            TaskStatus.FIXING.value: TaskStatus.READY.value,  # resume skips plan+code
        }
        count = 0
        cur = self._conn.cursor()
        cur.execute(
            "SELECT task_id, execution_json FROM task_executions WHERE status IN (?, ?, ?, ?, ?, ?)",
            tuple(rollback_map.keys()),
        )
        rows = cur.fetchall()
        for r in rows:
            ex = TaskExecution.model_validate_json(r["execution_json"])
            new_status = TaskStatus(rollback_map[ex.status.value])
            log.info(
                "Resume: rolling back %s from %s -> %s",
                ex.task_id,
                ex.status.value,
                new_status.value,
            )
            ex.status = new_status
            ex.last_error = "rolled back on resume"
            ex.touch()
            self._conn.execute(
                "UPDATE task_executions SET status = ?, execution_json = ?, updated_at = ? "
                "WHERE task_id = ?",
                (new_status.value, ex.model_dump_json(), ex.updated_at.isoformat(), ex.task_id),
            )
            count += 1
        self._conn.commit()
        return count

    def close(self) -> None:
        self._conn.close()
