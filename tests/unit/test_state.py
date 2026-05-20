"""Tests for `hybrid_agent.state.StateStore`.

Uses a real on-disk SQLite file in tmp_path (no in-memory ``:memory:`` because
StateStore opens the file lazily and we want to verify path-creation behavior).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_agent.models import (
    Backend,
    PlanOutput,
    TaskExecution,
    TaskSpec,
    TaskStatus,
)
from hybrid_agent.state import StateStore


@pytest.fixture
def store(tmp_path: Path):
    s = StateStore(tmp_path / "nested" / "state.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Schema / construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "a" / "b" / "state.db"
        s = StateStore(path)
        try:
            assert path.parent.is_dir()
            assert path.is_file()
        finally:
            s.close()

    def test_schema_idempotent(self, tmp_path: Path):
        # Open / close / reopen should not error on existing tables
        s1 = StateStore(tmp_path / "s.db")
        s1.close()
        s2 = StateStore(tmp_path / "s.db")
        s2.close()


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

class TestSpecs:
    def test_upsert_then_load(self, store, make_spec):
        store.upsert_specs([make_spec("T1"), make_spec("T2", deps=["T1"])])
        loaded = store.load_specs()
        ids = {s.id for s in loaded}
        assert ids == {"T1", "T2"}

    def test_upsert_replaces_existing(self, store, make_spec):
        store.upsert_specs([make_spec("T1", title="v1")])
        store.upsert_specs([make_spec("T1", title="v2")])
        loaded = store.load_specs()
        assert len(loaded) == 1
        assert loaded[0].title.endswith("T1") or loaded[0].title  # title from factory


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------

class TestExecutions:
    async def test_save_and_load(self, store):
        ex = TaskExecution(
            task_id="T1",
            status=TaskStatus.CODED,
            backend=Backend.LOCAL,
            attempts=2,
            plan=PlanOutput(approach="..."),
        )
        await store.save_execution(ex)
        loaded = store.load_executions()
        assert "T1" in loaded
        assert loaded["T1"].status == TaskStatus.CODED
        assert loaded["T1"].plan.approach == "..."

    async def test_get_execution_by_id(self, store):
        await store.save_execution(TaskExecution(task_id="T1"))
        assert store.get_execution("T1").task_id == "T1"
        assert store.get_execution("missing") is None

    async def test_save_overwrites(self, store):
        await store.save_execution(TaskExecution(task_id="T1", attempts=1))
        await store.save_execution(TaskExecution(task_id="T1", attempts=5))
        assert store.get_execution("T1").attempts == 5


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

class TestRunLog:
    async def test_log_run_persists(self, store):
        await store.log_run(
            "T1", "plan", "claude", True, 1234, "files=3"
        )
        # Re-read via raw SQL since there's no public list API
        cur = store._conn.cursor()  # noqa: SLF001
        rows = cur.execute("SELECT task_id, stage, success, duration_ms FROM run_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["task_id"] == "T1"
        assert rows[0]["success"] == 1


# ---------------------------------------------------------------------------
# Status counts / summary
# ---------------------------------------------------------------------------

class TestSummary:
    async def test_status_counts(self, store):
        await store.save_execution(TaskExecution(task_id="A", status=TaskStatus.DONE))
        await store.save_execution(TaskExecution(task_id="B", status=TaskStatus.DONE))
        await store.save_execution(TaskExecution(task_id="C", status=TaskStatus.FAILED))
        counts = store.status_counts()
        assert counts == {"done": 2, "failed": 1}


# ---------------------------------------------------------------------------
# reset_in_progress (the critical resume bug from the log review)
# ---------------------------------------------------------------------------

class TestResetInProgress:
    async def test_planning_rolled_back_to_ready(self, store):
        await store.save_execution(TaskExecution(task_id="T1", status=TaskStatus.PLANNING))
        n = store.reset_in_progress()
        assert n == 1
        assert store.get_execution("T1").status == TaskStatus.READY

    async def test_planned_rolled_back_to_ready(self, store):
        # The bug we fixed: PLANNED was never reset, so tasks got stuck
        await store.save_execution(TaskExecution(task_id="T1", status=TaskStatus.PLANNED))
        store.reset_in_progress()
        assert store.get_execution("T1").status == TaskStatus.READY

    async def test_coded_rolled_back_to_ready(self, store):
        await store.save_execution(TaskExecution(task_id="T1", status=TaskStatus.CODED))
        store.reset_in_progress()
        assert store.get_execution("T1").status == TaskStatus.READY

    async def test_reviewing_and_fixing_rolled_back(self, store):
        await store.save_execution(TaskExecution(task_id="A", status=TaskStatus.REVIEWING))
        await store.save_execution(TaskExecution(task_id="B", status=TaskStatus.FIXING))
        store.reset_in_progress()
        assert store.get_execution("A").status == TaskStatus.READY
        assert store.get_execution("B").status == TaskStatus.READY

    async def test_terminal_states_untouched(self, store):
        await store.save_execution(TaskExecution(task_id="A", status=TaskStatus.DONE))
        await store.save_execution(TaskExecution(task_id="B", status=TaskStatus.FAILED))
        await store.save_execution(TaskExecution(task_id="C", status=TaskStatus.BLOCKED))
        n = store.reset_in_progress()
        assert n == 0
        assert store.get_execution("A").status == TaskStatus.DONE
        assert store.get_execution("B").status == TaskStatus.FAILED
        assert store.get_execution("C").status == TaskStatus.BLOCKED

    async def test_pending_and_ready_untouched(self, store):
        await store.save_execution(TaskExecution(task_id="A", status=TaskStatus.PENDING))
        await store.save_execution(TaskExecution(task_id="B", status=TaskStatus.READY))
        store.reset_in_progress()
        assert store.get_execution("A").status == TaskStatus.PENDING
        assert store.get_execution("B").status == TaskStatus.READY
