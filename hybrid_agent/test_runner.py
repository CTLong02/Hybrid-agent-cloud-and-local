"""Run a project's test suite in a sandbox directory and classify the result.

Used by the reviewer pipeline to verify the worker's output before marking
a task DONE. Returns a `TestResult` — the caller decides what verdict to map
each status onto (today: anything-but-PASSED → NEEDS_FIX, with NO_TESTS as a
config-gated soft fail).

Runs the command directly on the host (no sandbox isolation) because the
agent is used for personal development. If you ever need to run untrusted
code, wrap this with Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NO_TESTS = "no_tests"
    TIMEOUT = "timeout"
    ERROR = "error"  # couldn't even start the process


@dataclass
class TestResult:
    status: TestStatus
    rc: int
    duration_s: float
    stdout: str
    stderr: str
    command: str

    @property
    def output(self) -> str:
        """Combined stdout+stderr, tail-capped, suitable for pasting into
        reviewer feedback. Test failures usually print useful info at the
        bottom of pytest output, so we keep the tail."""
        text = (self.stdout + "\n" + self.stderr).strip()
        if len(text) > 4000:
            return "...(truncated)...\n" + text[-4000:]
        return text


# pytest exit code 5 = no tests collected. Other runners signal it via stderr.
_NO_TESTS_PHRASES = (
    "no tests ran",
    "no tests collected",
    "no tests found",
    "no test files",
    "0 passed",
)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the entire process tree.

    On Windows, `proc.kill()` against a shell-spawned subprocess only kills
    the wrapping `cmd.exe` and leaves the actual test runner orphaned, so
    the timeout would still wait the full sleep duration. Use `taskkill /T`
    to nuke the whole tree.
    """
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_shell(
                f"taskkill /F /T /PID {proc.pid}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.warning("kill_tree failed for pid=%s: %s", proc.pid, exc)
    # Reap regardless so the OS releases pipes/handles.
    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


def _classify(rc: int, stdout: str, stderr: str) -> TestStatus:
    if rc == 0:
        # Some runners (jest, npm test default) exit 0 even when no tests
        # were found. Be conservative: only call it NO_TESTS if explicit.
        combined = (stdout + stderr).lower()
        if any(p in combined for p in ("no tests found", "no test files")):
            return TestStatus.NO_TESTS
        return TestStatus.PASSED

    # pytest's documented "no tests collected" exit code
    if rc == 5:
        return TestStatus.NO_TESTS

    combined = (stdout + "\n" + stderr).lower()
    if any(phrase in combined for phrase in _NO_TESTS_PHRASES):
        return TestStatus.NO_TESTS

    return TestStatus.FAILED


async def run_tests(
    sandbox_path: Path,
    command: str,
    *,
    timeout_seconds: int = 300,
) -> TestResult:
    """Run `command` in `sandbox_path`. Never raises on test failure — the
    failure is signalled in the returned `status`. Only raises on real
    bootstrap problems (process can't start)."""
    log.info(
        "running tests: %s (cwd=%s, timeout=%ds)",
        command,
        sandbox_path,
        timeout_seconds,
    )
    t0 = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(sandbox_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("failed to spawn test process: %s", exc)
        return TestResult(
            status=TestStatus.ERROR,
            rc=-1,
            duration_s=0.0,
            stdout="",
            stderr=f"failed to start test runner: {exc}",
            command=command,
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        return TestResult(
            status=TestStatus.TIMEOUT,
            rc=-1,
            duration_s=time.monotonic() - t0,
            stdout="",
            stderr=f"test command timed out after {timeout_seconds}s",
            command=command,
        )

    duration_s = time.monotonic() - t0
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    rc = proc.returncode or 0

    status = _classify(rc, stdout, stderr)
    log.info(
        "tests finished: status=%s rc=%d duration=%.1fs",
        status.value,
        rc,
        duration_s,
    )

    return TestResult(
        status=status,
        rc=rc,
        duration_s=duration_s,
        stdout=stdout,
        stderr=stderr,
        command=command,
    )
