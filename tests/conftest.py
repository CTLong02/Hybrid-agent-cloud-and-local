"""Shared pytest fixtures.

Anything that's likely to be reused across multiple test modules lives here:
deterministic dummy `TaskSpec` / `TaskExecution` builders, a fast retry config
(no real sleeping), and an in-memory state store factory. Test modules build
on top of these instead of repeating boilerplate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hybrid_agent.config import (
    AppConfig,
    ClaudeConfig,
    CostConfig,
    LocalConfig,
    LocalEndpointConfig,
    OrchestratorConfig,
    RetryConfig,
    RoutingConfig,
    SandboxConfig,
    StateConfig,
)
from hybrid_agent.models import (
    Backend,
    Complexity,
    TaskExecution,
    TaskSpec,
    TaskStatus,
)


@pytest.fixture
def fast_retry() -> RetryConfig:
    """Retry policy that finishes quickly even on exhaustion (no real sleeps)."""
    return RetryConfig(
        max_attempts=3,
        initial_delay_seconds=0.0,
        max_delay_seconds=0.0,
        exponential_base=1.0,
        jitter=False,
    )


@pytest.fixture
def base_config(tmp_path: Path) -> AppConfig:
    """A complete AppConfig anchored at a tmp project root."""
    return AppConfig(
        project_root=str(tmp_path),
        log_level="WARNING",
        log_file=None,
        log_json_file=None,
        state=StateConfig(db_path=str(tmp_path / "state.db")),
        sandbox=SandboxConfig(
            base_dir=".sandboxes",
            use_git_worktree=False,
            base_branch="main",
            branch_prefix="agent/",
            cleanup_on_success=False,
        ),
        retry=RetryConfig(
            max_attempts=2,
            initial_delay_seconds=0.0,
            max_delay_seconds=0.0,
            exponential_base=1.0,
            jitter=False,
        ),
        routing=RoutingConfig(
            default_backend="local",
            force_claude_tags=["claude_only"],
            force_local_tags=["local_only"],
            claude_for_complexity=["high"],
            escalate_to_claude_after_failures=2,
            claude_path_globs=[],
        ),
        orchestrator=OrchestratorConfig(
            poll_interval_seconds=0.01,
            max_concurrent_tasks=2,
            run_tests_in_review=False,
        ),
        cost=CostConfig(),
        local=LocalConfig(endpoints=[LocalEndpointConfig(name="local-test")]),
        claude=ClaudeConfig(enabled=True, concurrency=1),
    )


def _spec(
    tid: str,
    *,
    deps: list[str] | None = None,
    tags: list[str] | None = None,
    complexity: str = "low",
    files: list[str] | None = None,
    title: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        id=tid,
        title=title or f"Task {tid}",
        description="",
        tags=list(tags or []),
        complexity=Complexity(complexity),
        target_files=list(files or []),
        depends_on=list(deps or []),
        acceptance_criteria="",
    )


@pytest.fixture
def make_spec():
    """Factory: `make_spec("T1", deps=["T0"], tags=["auth"])`."""
    return _spec


@pytest.fixture
def make_execution():
    """Factory for TaskExecution rows in arbitrary states."""
    def _build(
        task_id: str,
        *,
        status: TaskStatus = TaskStatus.PENDING,
        attempts: int = 0,
        backend: Backend | None = None,
    ) -> TaskExecution:
        ex = TaskExecution(task_id=task_id, status=status, attempts=attempts)
        if backend is not None:
            ex.backend = backend
        return ex
    return _build


@pytest.fixture
def frozen_now() -> datetime:
    """Deterministic timestamp for tests that compare datetimes."""
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake LLM client + pool — used by planner / worker / reviewer / orchestrator
# tests so we don't need a real Ollama or Claude SDK at hand.
# ---------------------------------------------------------------------------

import asyncio
from contextlib import asynccontextmanager

from hybrid_agent.llm.base import LLMClient, LLMPool, LLMResult


class FakeLLMClient(LLMClient):
    """LLM client that returns pre-queued canned responses.

    Tests queue responses via `client.queue("text", "next text", ...)`.
    Each `complete()` call dequeues one. Exceptions queued in place of strings
    are raised when reached, letting tests simulate retriable / fatal errors.
    """

    def __init__(self, name: str = "fake-llm", default_model: str = "fake-model") -> None:
        self.name = name
        self.default_model = default_model
        self.responses: list = []
        self.calls: list[dict] = []

    def queue(self, *responses) -> None:
        self.responses.extend(responses)

    async def complete(  # type: ignore[override]
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs,
    ) -> LLMResult:
        self.calls.append({"system": system_prompt, "user": user_prompt, **kwargs})
        if not self.responses:
            raise RuntimeError(
                f"FakeLLMClient({self.name}): no canned response queued. "
                f"Call .queue(text) before triggering the call."
            )
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            r = r(self.calls[-1])
        return LLMResult(
            text=str(r),
            model=kwargs.get("model") or self.default_model,
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def health(self) -> bool:
        return True


class FakeLLMPool(LLMPool):
    """LLMPool that always yields the same FakeLLMClient.

    Subclasses LLMPool so type checks in production code (e.g. orchestrator)
    accept it as-is. The semaphore is real to preserve concurrency semantics.
    """

    def __init__(self, client: FakeLLMClient, concurrency: int = 1) -> None:
        super().__init__([client], concurrency=concurrency)


@pytest.fixture
def fake_local_client() -> FakeLLMClient:
    return FakeLLMClient(name="fake-local", default_model="qwen3-coder:30b")


@pytest.fixture
def fake_claude_client() -> FakeLLMClient:
    return FakeLLMClient(name="fake-claude", default_model="claude-sonnet-4-5")


@pytest.fixture
def fake_local_pool(fake_local_client) -> FakeLLMPool:
    return FakeLLMPool(fake_local_client)


@pytest.fixture
def fake_claude_pool(fake_claude_client) -> FakeLLMPool:
    return FakeLLMPool(fake_claude_client)
