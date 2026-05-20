"""End-to-end orchestrator pipeline tests with fully mocked LLMs.

Builds a real `Orchestrator` with `FakeLLMPool` injected for both backends, a
copy-mode sandbox in `tmp_path`, and a real SQLite state store. Exercises the
plan → code → review pipeline for 1–2 tasks and asserts terminal status +
filesystem effects.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_agent.models import Backend, ReviewVerdict, TaskStatus
from hybrid_agent.orchestrator import Orchestrator
from hybrid_agent.state import StateStore
from hybrid_agent.test_runner import TestResult, TestStatus


# ---------------------------------------------------------------------------
# Helpers — canned responses for each pipeline stage
# ---------------------------------------------------------------------------

def plan_response(*, files_to_create=None, files_to_modify=None) -> str:
    return json.dumps({
        "files_to_modify": files_to_modify or [],
        "files_to_create": files_to_create or [],
        "approach": "create the file",
        "test_strategy": "pytest",
        "estimated_loc": 10,
        "context_snippets": {},
    })


def local_code_response(path: str, content: str = "x = 1\n") -> str:
    return json.dumps({
        "summary": f"created {path}",
        "files": [{"path": path, "operation": "create", "content": content}],
        "tests": [],
    })


def review_approved() -> str:
    return json.dumps({
        "verdict": "approved",
        "score": 0.95,
        "issues": [],
        "auto_fixed": False,
        "summary": "looks good",
    })


# ---------------------------------------------------------------------------
# Fixture: orchestrator with FakeLLMPools and stubbed-out test gate
# ---------------------------------------------------------------------------

@pytest.fixture
def orchestrator_factory(base_config, fake_local_pool, fake_claude_pool, monkeypatch, tmp_path):
    """Returns a function `build(specs)` that produces a configured Orchestrator."""

    # Disable test gate in orchestrator e2e — gate has its own dedicated tests
    base_config.orchestrator.run_tests_in_review = False

    # Patch the pool builders so the orchestrator picks up our fakes
    monkeypatch.setattr(
        "hybrid_agent.orchestrator.build_local_pool",
        lambda cfg: fake_local_pool,
    )
    monkeypatch.setattr(
        "hybrid_agent.orchestrator.build_claude_pool",
        lambda cfg: fake_claude_pool,
    )

    def _build(specs):
        state = StateStore(tmp_path / "state.db")
        return Orchestrator(base_config, specs, state), state

    return _build


# ---------------------------------------------------------------------------
# Single-task pipeline
# ---------------------------------------------------------------------------

class TestSingleTaskPipeline:
    async def test_plan_code_review_done_local_backend(
        self, orchestrator_factory, fake_local_client, fake_claude_client,
        make_spec,
    ):
        spec = make_spec("T1", title="create foo")
        orch, state = orchestrator_factory([spec])

        # Stage 1: planner (Claude) → returns a plan
        fake_claude_client.queue(plan_response(files_to_create=["src/foo.py"]))
        # Stage 2: local worker → returns file map
        fake_local_client.queue(local_code_response("src/foo.py", "def foo():\n    return 1\n"))
        # Stage 3: reviewer (Claude) → approved
        fake_claude_client.queue(review_approved())

        await orch.run()

        # Final state: T1 done, file actually written
        assert orch.executions["T1"].status == TaskStatus.DONE
        assert orch.executions["T1"].backend == Backend.LOCAL
        sandbox = Path(orch.executions["T1"].sandbox_path)
        assert (sandbox / "src" / "foo.py").read_text() == "def foo():\n    return 1\n"

    async def test_force_claude_tag_routes_to_claude_worker(
        self, orchestrator_factory, fake_claude_client, make_spec,
    ):
        # ClaudeWorker uses tools — we just need any text containing SUMMARY
        spec = make_spec("T1", tags=["claude_only"])
        orch, _ = orchestrator_factory([spec])

        fake_claude_client.queue(plan_response())                 # plan
        fake_claude_client.queue("did the work\nSUMMARY: ok")     # code
        fake_claude_client.queue(review_approved())               # review

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.DONE
        assert orch.executions["T1"].backend == Backend.CLAUDE


# ---------------------------------------------------------------------------
# Multi-task with deps
# ---------------------------------------------------------------------------

class TestDependencyExecution:
    async def test_two_tasks_sequenced_by_deps(
        self, orchestrator_factory, fake_local_client, fake_claude_client, make_spec,
    ):
        # T2 depends on T1 — must run after
        a = make_spec("T1", title="first")
        b = make_spec("T2", title="second", deps=["T1"])
        orch, _ = orchestrator_factory([a, b])

        # T1 pipeline
        fake_claude_client.queue(plan_response(files_to_create=["a.py"]))
        fake_local_client.queue(local_code_response("a.py", "A"))
        fake_claude_client.queue(review_approved())
        # T2 pipeline
        fake_claude_client.queue(plan_response(files_to_create=["b.py"]))
        fake_local_client.queue(local_code_response("b.py", "B"))
        fake_claude_client.queue(review_approved())

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.DONE
        assert orch.executions["T2"].status == TaskStatus.DONE
        # T1 must have started before T2 (started_at order)
        assert orch.executions["T1"].started_at <= orch.executions["T2"].started_at

    async def test_failure_blocks_downstream(
        self, orchestrator_factory, fake_local_client, fake_claude_client, make_spec,
    ):
        a = make_spec("T1")
        b = make_spec("T2", deps=["T1"])
        orch, _ = orchestrator_factory([a, b])

        # T1 plan succeeds, but worker raises an unrecoverable error every retry
        fake_claude_client.queue(plan_response())
        fake_local_client.queue(*[Exception("worker exploded")] * 10)

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.FAILED
        assert orch.executions["T2"].status == TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# Review-iteration loop
# ---------------------------------------------------------------------------

class TestReviewIteration:
    async def test_needs_fix_then_approved_succeeds(
        self, orchestrator_factory, fake_local_client, fake_claude_client, make_spec,
    ):
        spec = make_spec("T1")
        orch, _ = orchestrator_factory([spec])
        # Allow up to 2 review iterations
        orch.config.orchestrator.review_max_iterations = 2

        fake_claude_client.queue(plan_response(files_to_create=["a.py"]))
        fake_local_client.queue(local_code_response("a.py", "v1"))
        # First review: needs_fix → orchestrator re-runs worker
        fake_claude_client.queue(json.dumps({
            "verdict": "needs_fix",
            "issues": ["please return v2 instead"],
            "auto_fixed": False,
        }))
        fake_local_client.queue(local_code_response("a.py", "v2"))
        # Second review: approved
        fake_claude_client.queue(review_approved())

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.DONE
        # Worker called twice (initial + fix)
        assert orch.executions["T1"].review_iterations == 2

    async def test_max_iterations_exhausted_fails(
        self, orchestrator_factory, fake_local_client, fake_claude_client, make_spec,
    ):
        spec = make_spec("T1")
        orch, _ = orchestrator_factory([spec])
        orch.config.orchestrator.review_max_iterations = 2

        fake_claude_client.queue(plan_response(files_to_create=["a.py"]))
        fake_local_client.queue(local_code_response("a.py"))
        # Both reviews say needs_fix → task fails
        fake_claude_client.queue(json.dumps({"verdict": "needs_fix", "issues": ["nope"]}))
        fake_local_client.queue(local_code_response("a.py"))
        fake_claude_client.queue(json.dumps({"verdict": "needs_fix", "issues": ["still nope"]}))

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Resume + state persistence
# ---------------------------------------------------------------------------

class TestResume:
    async def test_planned_state_resumes_to_review(
        self, orchestrator_factory, fake_local_client, fake_claude_client, make_spec,
    ):
        """After our state.py fix, a task left in PLANNED on resume should
        roll back to READY and re-enter pipeline (skipping plan since it's saved)."""
        spec = make_spec("T1")
        orch, state = orchestrator_factory([spec])

        # Pre-seed the state DB as if a prior run had reached PLANNED
        from hybrid_agent.models import PlanOutput, TaskExecution
        await state.save_execution(TaskExecution(
            task_id="T1",
            status=TaskStatus.PLANNED,
            plan=PlanOutput(approach="...", files_to_create=["a.py"]),
        ))

        # No plan call needed — orchestrator should skip planning since plan exists.
        # Just queue worker + reviewer.
        fake_local_client.queue(local_code_response("a.py"))
        fake_claude_client.queue(review_approved())

        await orch.run()
        assert orch.executions["T1"].status == TaskStatus.DONE
        # Planner was NOT called (no entries in fake_claude_client.calls for plan)
        # but reviewer WAS — verify by checking nothing requested allowed_tools
        # with Read+Grep+Glob only (which is the planner's signature).
