"""Tests for `hybrid_agent.models`."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hybrid_agent.models import (
    Backend,
    CodeOutput,
    Complexity,
    FileChange,
    PlanOutput,
    ReviewOutput,
    ReviewVerdict,
    TaskExecution,
    TaskSpec,
    TaskStatus,
)


class TestTaskStatus:
    @pytest.mark.parametrize(
        "status",
        [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED],
    )
    def test_terminal_states(self, status):
        assert status.is_terminal is True
        assert status.is_in_progress is False

    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.PLANNING, TaskStatus.CODING,
            TaskStatus.REVIEWING, TaskStatus.FIXING,
        ],
    )
    def test_in_progress_states(self, status):
        assert status.is_in_progress is True
        assert status.is_terminal is False

    @pytest.mark.parametrize(
        "status",
        [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.PLANNED, TaskStatus.CODED],
    )
    def test_quiescent_states(self, status):
        assert status.is_terminal is False
        assert status.is_in_progress is False


class TestTaskSpec:
    def test_minimal_spec(self):
        s = TaskSpec(id="T1", title="x")
        assert s.id == "T1"
        assert s.depends_on == []
        assert s.tags == []
        assert s.complexity == Complexity.MEDIUM

    def test_complexity_string_coerces_to_enum(self):
        s = TaskSpec(id="T1", title="x", complexity="high")
        assert s.complexity == Complexity.HIGH

    def test_invalid_complexity_raises(self):
        with pytest.raises(ValidationError):
            TaskSpec(id="T1", title="x", complexity="ultra-mega")

    def test_round_trip_json(self):
        s = TaskSpec(
            id="T1", title="x", tags=["a", "b"],
            depends_on=["T0"], target_files=["a.py"],
        )
        s2 = TaskSpec.model_validate_json(s.model_dump_json())
        assert s2 == s


class TestPipelineOutputs:
    def test_plan_default_empty(self):
        p = PlanOutput()
        assert p.files_to_modify == []
        assert p.context_snippets == {}

    def test_code_with_filechanges(self):
        c = CodeOutput(
            branch="agent/T1",
            files_changed=[
                FileChange(path="a.py", operation="create", content="x = 1\n"),
                FileChange(path="b.py", operation="modify", content="y = 2\n"),
            ],
            summary="created two files",
        )
        assert len(c.files_changed) == 2
        assert c.files_changed[0].operation == "create"

    def test_review_verdict_enum(self):
        r = ReviewOutput(verdict="approved")  # string coerces
        assert r.verdict == ReviewVerdict.APPROVED


class TestTaskExecution:
    def test_default_state(self):
        ex = TaskExecution(task_id="T1")
        assert ex.status == TaskStatus.PENDING
        assert ex.attempts == 0
        assert ex.backend is None

    def test_touch_updates_timestamp(self):
        ex = TaskExecution(task_id="T1")
        before = ex.updated_at
        time.sleep(0.01)
        ex.touch()
        assert ex.updated_at > before

    def test_round_trip_with_nested_outputs(self):
        ex = TaskExecution(
            task_id="T1",
            status=TaskStatus.CODED,
            backend=Backend.CLAUDE,
            plan=PlanOutput(approach="...", files_to_create=["a.py"]),
            code=CodeOutput(branch="b", summary="ok"),
            started_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
        )
        round_tripped = TaskExecution.model_validate_json(ex.model_dump_json())
        assert round_tripped.status == TaskStatus.CODED
        assert round_tripped.plan.approach == "..."
        assert round_tripped.code.summary == "ok"
        assert round_tripped.started_at == ex.started_at
