"""Tests for `hybrid_agent.test_runner`.

Uses real subprocess but with fast, hermetic commands (`python -c ...`,
`exit N`) so the suite stays under a second.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hybrid_agent.test_runner import (
    TestResult,
    TestStatus,
    _classify,
    run_tests,
)


PY = sys.executable  # current python interpreter, deterministic


# ---------------------------------------------------------------------------
# _classify (pure function)
# ---------------------------------------------------------------------------

class TestClassify:
    def test_rc_zero_is_passed(self):
        assert _classify(0, "1 passed", "") == TestStatus.PASSED

    def test_rc_zero_with_no_tests_phrase_is_no_tests(self):
        assert _classify(0, "No tests found", "") == TestStatus.NO_TESTS

    def test_rc_5_is_pytest_no_tests_collected(self):
        assert _classify(5, "", "") == TestStatus.NO_TESTS

    def test_rc_1_failed(self):
        assert _classify(1, "1 failed", "") == TestStatus.FAILED

    def test_phrase_no_tests_ran_in_output(self):
        assert _classify(1, "no tests ran", "") == TestStatus.NO_TESTS

    def test_unknown_nonzero_is_failed(self):
        assert _classify(127, "command not found", "") == TestStatus.FAILED


# ---------------------------------------------------------------------------
# run_tests — happy paths
# ---------------------------------------------------------------------------

class TestRunTests:
    async def test_passing_command(self, tmp_path: Path):
        result = await run_tests(tmp_path, f'"{PY}" -c "print(\'ok\')"', timeout_seconds=10)
        assert result.status == TestStatus.PASSED
        assert result.rc == 0
        assert "ok" in result.stdout

    async def test_failing_command(self, tmp_path: Path):
        result = await run_tests(tmp_path, f'"{PY}" -c "import sys; sys.exit(1)"', timeout_seconds=10)
        assert result.status == TestStatus.FAILED
        assert result.rc == 1

    async def test_timeout_kills_process(self, tmp_path: Path):
        # 10s sleep, but timeout=1s — must come back within ~3s thanks to kill_tree
        import time
        t0 = time.monotonic()
        result = await run_tests(
            tmp_path,
            f'"{PY}" -c "import time; time.sleep(10)"',
            timeout_seconds=1,
        )
        elapsed = time.monotonic() - t0
        assert result.status == TestStatus.TIMEOUT
        assert elapsed < 5, f"took {elapsed}s, kill tree might be broken"

    async def test_command_captures_stdout_and_stderr(self, tmp_path: Path):
        result = await run_tests(
            tmp_path,
            f'"{PY}" -c "import sys; print(\'O\'); print(\'E\', file=sys.stderr); sys.exit(2)"',
            timeout_seconds=10,
        )
        assert "O" in result.stdout
        assert "E" in result.stderr
        assert result.rc == 2

    async def test_output_property_combines_and_caps(self, tmp_path: Path):
        # Generate >4000 chars of output to trigger truncation
        big_print = (
            f'"{PY}" -c "print(\'x\' * 5000); import sys; sys.exit(1)"'
        )
        result = await run_tests(tmp_path, big_print, timeout_seconds=10)
        assert len(result.output) <= 4100  # 4000 + small prefix
        assert "(truncated)" in result.output


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    async def test_invalid_command_returns_failed(self, tmp_path: Path):
        # Nonexistent binary — shell returns non-zero, classified FAILED
        result = await run_tests(
            tmp_path, "this_binary_does_not_exist_12345", timeout_seconds=5,
        )
        assert result.status in (TestStatus.FAILED, TestStatus.ERROR)
        assert result.rc != 0
