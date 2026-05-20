"""Tests for `hybrid_agent.agents.reviewer.Reviewer`.

Covers both the LLM-driven verdict and the test-gate that follows it (the
gate is what blocks shipping a task whose tests fail).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_agent.agents.reviewer import Reviewer, _extract_last_json
from hybrid_agent.models import (
    CodeOutput,
    PlanOutput,
    ReviewVerdict,
    TaskSpec,
)
from hybrid_agent.test_runner import TestResult, TestStatus


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestExtractLastJson:
    def test_single_object(self):
        assert _extract_last_json('{"verdict": "approved"}') == {"verdict": "approved"}

    def test_picks_last_when_multiple(self):
        # Reviewer narration may contain JSON-ish stuff; we want the FINAL one
        text = '{"first": 1}\nthen reasoning\n{"verdict": "approved"}'
        assert _extract_last_json(text) == {"verdict": "approved"}

    def test_strips_fences(self):
        text = '```json\n{"verdict": "approved"}\n```'
        assert _extract_last_json(text) == {"verdict": "approved"}

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="no parseable JSON"):
            _extract_last_json("just prose")


# ---------------------------------------------------------------------------
# Reviewer.review() — happy path & JSON parse failures
# ---------------------------------------------------------------------------

@pytest.fixture
def reviewer(base_config, fake_claude_pool):
    base_config.orchestrator.run_tests_in_review = False  # gate tested separately
    return Reviewer(fake_claude_pool, base_config)


@pytest.fixture
def plan() -> PlanOutput:
    return PlanOutput(approach="x")


@pytest.fixture
def code() -> CodeOutput:
    return CodeOutput(branch="agent/T1", summary="implemented foo")


@pytest.fixture
def spec(make_spec) -> TaskSpec:
    return make_spec("T1")


class TestReview:
    async def test_approved_verdict(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        fake_claude_client.queue(json.dumps({
            "verdict": "approved",
            "score": 0.95,
            "issues": [],
            "auto_fixed": False,
            "summary": "looks good",
        }))
        result = await reviewer.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.APPROVED
        assert result.score == 0.95
        assert result.auto_fixed is False

    async def test_needs_fix_with_issues(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        fake_claude_client.queue(json.dumps({
            "verdict": "needs_fix",
            "issues": ["missing tests", "wrong indent"],
            "summary": "fix and retry",
        }))
        result = await reviewer.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX
        assert "missing tests" in result.issues

    async def test_unparseable_returns_needs_fix(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        # The bug from the log: reviewer outputs only narration, no JSON
        fake_claude_client.queue(
            "I'll review the implemented JWT and password-hashing utilities..."
        )
        result = await reviewer.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX
        assert any("unparseable" in i for i in result.issues)

    async def test_invalid_verdict_value_falls_back_to_needs_fix(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        fake_claude_client.queue(json.dumps({"verdict": "nuke-from-orbit"}))
        result = await reviewer.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX

    async def test_diff_included_in_prompt(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))
        await reviewer.review(spec, plan, code, tmp_path, diff="diff --git a/x b/x\n+y")
        prompt = fake_claude_client.calls[0]["user"]
        assert "diff --git" in prompt

    async def test_diff_truncated_when_huge(self, reviewer, fake_claude_client, spec, plan, code, tmp_path):
        huge_diff = "+x\n" * 20_000  # ~80KB
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))
        await reviewer.review(spec, plan, code, tmp_path, diff=huge_diff)
        prompt = fake_claude_client.calls[0]["user"]
        assert "truncated" in prompt


# ---------------------------------------------------------------------------
# Test gate (the part that runs pytest after the LLM review)
# ---------------------------------------------------------------------------

class TestTestGate:
    @pytest.fixture
    def reviewer_with_gate(self, base_config, fake_claude_pool):
        base_config.orchestrator.run_tests_in_review = True
        base_config.orchestrator.test_command = "fake-cmd"  # we'll mock run_tests
        return Reviewer(fake_claude_pool, base_config)

    async def test_passing_tests_keep_approved(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.PASSED, rc=0, duration_s=0.1,
                stdout="1 passed", stderr="", command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.APPROVED

    async def test_failing_tests_downgrade_to_needs_fix(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        # LLM said APPROVED but tests fail — gate must override
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.FAILED, rc=1, duration_s=0.1,
                stdout="FAILED test_x.py::test_y", stderr="AssertionError",
                command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX
        assert any("Tests failed" in i for i in result.issues)
        assert any("test_x.py" in i for i in result.issues)

    async def test_timeout_blocks_done(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.TIMEOUT, rc=-1, duration_s=300.0,
                stdout="", stderr="timed out", command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX

    async def test_no_tests_with_require_false_keeps_approved(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        reviewer_with_gate.config.orchestrator.require_tests = False
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.NO_TESTS, rc=5, duration_s=0.1,
                stdout="", stderr="", command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.APPROVED

    async def test_no_tests_with_require_true_blocks(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        reviewer_with_gate.config.orchestrator.require_tests = True
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.NO_TESTS, rc=5, duration_s=0.1,
                stdout="", stderr="", command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX
        assert any("No tests found" in i for i in result.issues)

    async def test_no_language_marker_skips_gate(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path,
    ):
        # No test_command set + empty dir → detect_language returns None → gate skipped
        reviewer_with_gate.config.orchestrator.test_command = None
        reviewer_with_gate.config.orchestrator.require_tests = False
        fake_claude_client.queue(json.dumps({"verdict": "approved"}))
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.APPROVED

    async def test_already_needs_fix_still_appends_test_output(
        self, reviewer_with_gate, fake_claude_client, spec, plan, code, tmp_path, monkeypatch,
    ):
        fake_claude_client.queue(json.dumps({
            "verdict": "needs_fix",
            "issues": ["llm-found-issue"],
        }))

        async def fake_run_tests(*a, **kw):
            return TestResult(
                status=TestStatus.FAILED, rc=1, duration_s=0.1,
                stdout="FAILED", stderr="", command="fake-cmd",
            )

        monkeypatch.setattr("hybrid_agent.agents.reviewer.run_tests", fake_run_tests)
        result = await reviewer_with_gate.review(spec, plan, code, tmp_path, diff="")
        assert result.verdict == ReviewVerdict.NEEDS_FIX
        # Should contain BOTH the LLM issue AND the test failure
        assert any("llm-found-issue" in i for i in result.issues)
        assert any("Tests failed" in i for i in result.issues)
