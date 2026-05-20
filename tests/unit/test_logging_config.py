"""Tests for `hybrid_agent.logging_config`."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from hybrid_agent.logging_config import (
    get_logger,
    get_trace_field,
    new_run_id,
    setup_logging,
    trace_context,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test gets a clean root logger so handlers from prior tests don't leak."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


class TestRunId:
    def test_unique(self):
        ids = {new_run_id() for _ in range(50)}
        assert len(ids) == 50

    def test_short(self):
        rid = new_run_id()
        assert len(rid) == 8
        assert rid.isalnum()


class TestTraceContext:
    def test_field_returns_none_outside_context(self):
        assert get_trace_field("task_id") is None

    def test_field_set_inside_context(self):
        with trace_context(task_id="T42"):
            assert get_trace_field("task_id") == "T42"
        assert get_trace_field("task_id") is None

    def test_nested_inherits_parent(self):
        with trace_context(run_id="R1"):
            with trace_context(task_id="T1"):
                assert get_trace_field("run_id") == "R1"
                assert get_trace_field("task_id") == "T1"

    def test_inner_overrides_outer(self):
        with trace_context(task_id="T1"):
            with trace_context(task_id="T2"):
                assert get_trace_field("task_id") == "T2"
            # Reverts to outer
            assert get_trace_field("task_id") == "T1"

    def test_none_values_are_dropped(self):
        with trace_context(task_id="T1", attempt=None):
            assert get_trace_field("task_id") == "T1"
            assert get_trace_field("attempt") is None  # never set

    async def test_propagates_through_create_task(self):
        captured: list[str] = []

        async def child():
            captured.append(get_trace_field("task_id"))

        with trace_context(task_id="T_async"):
            await asyncio.create_task(child())

        assert captured == ["T_async"]


class TestSetupLogging:
    def test_setup_is_idempotent(self, tmp_path: Path):
        # Two consecutive setups shouldn't double the handlers
        setup_logging(level="INFO", log_file=str(tmp_path / "a.log"))
        setup_logging(level="INFO", log_file=str(tmp_path / "b.log"))
        # Only the second setup's handlers remain on root
        root = logging.getLogger()
        # Console + b.log = 2 handlers (a.log was removed)
        assert len(root.handlers) == 2

    def test_json_file_writes_valid_jsonl(self, tmp_path: Path):
        json_path = tmp_path / "log.jsonl"
        setup_logging(level="INFO", json_file=str(json_path))

        log = logging.getLogger("hybrid_agent.test_logging")
        with trace_context(task_id="T1", run_id="R1"):
            log.info("hello")

        # Force flush
        for h in logging.getLogger().handlers:
            h.flush()

        lines = [
            json.loads(ln) for ln in json_path.read_text().splitlines() if ln.strip()
        ]
        assert len(lines) == 1
        record = lines[0]
        assert record["event"] == "hello"
        assert record["task_id"] == "T1"
        assert record["run_id"] == "R1"
        assert record["level"] == "info"

    def test_native_structlog_logger_with_kwargs(self, tmp_path: Path):
        json_path = tmp_path / "log.jsonl"
        setup_logging(level="INFO", json_file=str(json_path))

        log = get_logger("hybrid_agent.struct_test")
        log.info("native", model="claude", tokens=42)

        for h in logging.getLogger().handlers:
            h.flush()

        records = [
            json.loads(ln) for ln in json_path.read_text().splitlines() if ln.strip()
        ]
        target = next(r for r in records if r["event"] == "native")
        assert target["model"] == "claude"
        assert target["tokens"] == 42
