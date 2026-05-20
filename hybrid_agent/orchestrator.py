"""Orchestrator: end-to-end coordination loop.

Runs the pipeline (plan → code → review → done) for every task, respecting
DAG dependencies and pool concurrency. Persists state on every transition
so resume works even on hard kills.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from .agents import ClaudeWorker, LocalWorker, Planner, Reviewer
from .config import AppConfig
from .cost import CostMeter, cost_context, format_summary
from .dag import TaskDAG
from .llm import LLMPool, build_claude_pool, build_local_pool
from .logging_config import new_run_id, trace_context
from .models import (
    Backend,
    ReviewVerdict,
    TaskExecution,
    TaskSpec,
    TaskStatus,
)
from .retry import retry_async
from .routing import Router, RoutingContext, build_default_router
from .sandbox import SandboxManager
from .state import StateStore

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        specs: list[TaskSpec],
        state: StateStore,
    ) -> None:
        self.config = config
        self.state = state
        self.dag = TaskDAG(specs)
        self.specs_by_id = {s.id: s for s in specs}

        # In-memory mirror of executions (still backed by SQLite)
        self.executions: dict[str, TaskExecution] = {}

        # LLM pools
        self.local_pool: LLMPool = build_local_pool(config)
        claude_pool = build_claude_pool(config)
        if claude_pool is None:
            raise RuntimeError("Claude pool is required (planner + reviewer use Claude).")
        self.claude_pool: LLMPool = claude_pool

        # Agents
        self.planner = Planner(self.claude_pool, config)
        self.reviewer = Reviewer(self.claude_pool, config)
        self.local_worker = LocalWorker(self.local_pool, config)
        self.claude_worker = ClaudeWorker(self.claude_pool, config)

        # Routing
        self.router: Router = build_default_router(config.routing)

        # Sandbox
        self.sandbox = SandboxManager(config.project_root_path(), config.sandbox)

        # Concurrency
        self.task_sem = asyncio.Semaphore(config.orchestrator.max_concurrent_tasks)
        self._shutdown = asyncio.Event()
        self._running_tasks: set[asyncio.Task] = set()

        # Per-CLI-invocation id (stamped on every log line via contextvars)
        self.run_id = new_run_id()

        # Cost meter for this run; LLM clients charge into it via contextvar.
        self.cost_meter = CostMeter(config.cost)

    # ------------------------------------------------------------------
    # Setup / resume
    # ------------------------------------------------------------------

    def initialize_state(self) -> None:
        """Ensure every spec has an execution row; resume in-progress tasks."""
        self.state.upsert_specs(list(self.specs_by_id.values()))
        existing = self.state.load_executions()

        # Reset stale in-progress states
        reset = self.state.reset_in_progress()
        if reset:
            log.info("Resume: rolled back %d in-progress tasks", reset)
            existing = self.state.load_executions()

        for tid in self.specs_by_id:
            if tid not in existing:
                ex = TaskExecution(task_id=tid, status=TaskStatus.PENDING)
                self.executions[tid] = ex
            else:
                self.executions[tid] = existing[tid]

        # Promote PENDING/BLOCKED -> READY where every dep is DONE.
        #
        # BLOCKED is included so that after a previous run failed an upstream
        # task, a successful resume of that upstream automatically frees its
        # dependents — the user shouldn't have to `cli reset` every BLOCKED
        # task by hand. We don't touch FAILED here (operator intent: the user
        # decides when to retry a hard failure via `reset`).
        revived = 0
        for tid, ex in self.executions.items():
            if ex.status not in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                continue
            deps = self.specs_by_id[tid].depends_on
            if all(
                self.executions.get(d) is not None
                and self.executions[d].status == TaskStatus.DONE
                for d in deps
            ):
                if ex.status == TaskStatus.BLOCKED:
                    log.info("Resume: %s revived (BLOCKED -> READY)", tid)
                    ex.last_error = ""
                    revived += 1
                ex.status = TaskStatus.READY
        if revived:
            log.info("Resume: revived %d BLOCKED task(s) whose deps are now DONE", revived)

    async def _persist_all(self) -> None:
        for ex in self.executions.values():
            await self.state.save_execution(ex)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        with trace_context(run_id=self.run_id), cost_context(self.cost_meter):
            try:
                await self._run_loop()
            finally:
                # Always log the cost summary, even on crash, so operators
                # see what was already spent.
                for line in format_summary(self.cost_meter).splitlines():
                    log.info(line)

    async def _run_loop(self) -> None:
        self.initialize_state()
        await self._persist_all()
        self._install_signal_handlers()

        # Health check
        local_ok = await self.local_pool.health()
        claude_ok = await self.claude_pool.health()
        log.info("Pools health — local: %s, claude: %s", local_ok, claude_ok)
        if not local_ok and not claude_ok:
            raise RuntimeError("No LLM backend is healthy. Aborting.")

        log.info(
            "Starting with %d tasks (topo order: %s)",
            len(self.specs_by_id),
            self.dag.topo_order(),
        )

        try:
            while not self._shutdown.is_set():
                # Mark tasks blocked by failed deps
                for tid in self.dag.get_blocked(self.executions):
                    ex = self.executions[tid]
                    if ex.status != TaskStatus.BLOCKED:
                        log.warning("Task %s BLOCKED (upstream failed)", tid)
                        ex.status = TaskStatus.BLOCKED
                        ex.last_error = "upstream dependency failed"
                        await self.state.save_execution(ex)

                # Schedule ready tasks. `get_ready` returns tasks in PENDING
                # OR READY state whose deps are DONE — both must be eligible
                # since deps becoming satisfied mid-run doesn't promote
                # PENDING -> READY automatically.
                ready_ids = self.dag.get_ready(self.executions)
                for tid in ready_ids:
                    ex = self.executions[tid]
                    if ex.status in (TaskStatus.PENDING, TaskStatus.READY):
                        # Pre-mark as PLANNING so get_ready() won't return this
                        # task again on the next poll while the coroutine is
                        # queued but hasn't acquired the semaphore yet.
                        ex.status = TaskStatus.PLANNING
                        coro = self._execute_pipeline(tid)
                        task = asyncio.create_task(coro, name=f"pipeline-{tid}")
                        self._running_tasks.add(task)
                        task.add_done_callback(self._running_tasks.discard)

                # Termination check
                if self.dag.all_terminal(self.executions) and not self._running_tasks:
                    log.info("All tasks terminal. Done.")
                    break

                # Wait briefly before next poll
                await asyncio.sleep(self.config.orchestrator.poll_interval_seconds)

        finally:
            # Drain in-flight
            if self._running_tasks:
                log.info("Draining %d running tasks…", len(self._running_tasks))
                await asyncio.gather(*self._running_tasks, return_exceptions=True)
            await self._persist_all()
            self._summary()

    # ------------------------------------------------------------------
    # Per-task pipeline
    # ------------------------------------------------------------------

    def _migration_source_branch(self, spec) -> str | None:
        """Return the branch a migration task should fork from, or None.

        A task is a migration when it carries the ``migration`` tag. The
        sandbox is then forked from the recorded sandbox_branch of its first
        DONE dependency, so the worker sees and edits that upstream task's
        artifacts in place — the whole point of the migration mode.

        Falls back to None (caller will use base) when:
          - the tag isn't present, or
          - the spec has no depends_on, or
          - the first dep isn't DONE yet (orchestrator should have waited,
            but be defensive), or
          - the first dep doesn't have a recorded branch (e.g. non-git
            sandbox fallback).
        """
        if "migration" not in spec.tags:
            return None
        if not spec.depends_on:
            return None
        dep_id = spec.depends_on[0]
        dep_ex = self.executions.get(dep_id)
        if dep_ex is None or dep_ex.status != TaskStatus.DONE:
            return None
        return dep_ex.sandbox_branch or None

    def _stage_timeout(self, spec, stage: str) -> int:
        """Resolve the per-stage timeout, scaled by the task's complexity.

        `complexity_timeout_multiplier` maps complexity -> multiplier. Unknown
        complexities fall back to 1.0 (base timeout, unchanged).
        """
        cfg = self.config.orchestrator
        base = {
            "plan": cfg.plan_timeout_seconds,
            "code": cfg.code_timeout_seconds,
            "review": cfg.review_timeout_seconds,
        }[stage]
        mult = cfg.complexity_timeout_multiplier.get(spec.complexity.value, 1.0)
        return max(1, int(base * mult))

    async def _execute_pipeline(self, task_id: str) -> None:
        """Run plan → code → review for a single task. Owns its own task semaphore slot."""
        async with self.task_sem:
            spec = self.specs_by_id[task_id]
            ex = self.executions[task_id]
            ex.attempts += 1
            ex.started_at = ex.started_at or datetime.now(timezone.utc)

            # Bind task_id + attempt onto the trace context so every log line
            # downstream (planner, worker, reviewer, sandbox) carries them.
            with trace_context(task_id=task_id, attempt=ex.attempts):
                await self._pipeline_body(task_id, spec, ex)

    async def _pipeline_body(self, task_id, spec, ex) -> None:
        # Tracked here so the TimeoutError handler below can report which
        # stage hit the wall and what budget it had — `asyncio.TimeoutError`
        # has no message of its own.
        current_stage = "init"
        current_timeout = 0
        try:
            # ---- Plan -------------------------------------------------
            if ex.plan is None:
                with trace_context(stage="plan"):
                    await self._set_status(ex, TaskStatus.PLANNING)
                    t0 = time.monotonic()
                    current_stage = "plan"
                    current_timeout = self._stage_timeout(spec, "plan")
                    plan = await asyncio.wait_for(
                        retry_async(
                            self.planner.plan,
                            spec,
                            self.config.project_root_path(),
                            config=self.config.retry,
                            op_name=f"plan({task_id})",
                        ),
                        timeout=current_timeout,
                    )
                    ex.plan = plan
                    await self._set_status(ex, TaskStatus.PLANNED)
                    await self.state.log_run(
                        task_id,
                        "plan",
                        "claude",
                        True,
                        int((time.monotonic() - t0) * 1000),
                        f"files_to_modify={len(plan.files_to_modify)}, "
                        f"files_to_create={len(plan.files_to_create)}",
                    )

            # ---- Sandbox ---------------------------------------------
            if not ex.sandbox_path:
                from_branch = self._migration_source_branch(spec)
                sandbox_path, branch = await self.sandbox.create(
                    task_id, from_branch=from_branch
                )
                if from_branch:
                    log.info(
                        "Task %s is a migration on top of %s; sandbox forked from %s",
                        task_id,
                        spec.depends_on[0] if spec.depends_on else "?",
                        from_branch,
                    )
                ex.sandbox_path = str(sandbox_path)
                ex.sandbox_branch = branch
                await self.state.save_execution(ex)
            else:
                sandbox_path = Path(ex.sandbox_path)
                branch = ex.sandbox_branch

            # ---- Route + Code ----------------------------------------
            if ex.code is None:
                with trace_context(stage="code"):
                    decision = self.router.route(
                        spec,
                        ex,
                        RoutingContext(
                            local_available=any(
                                self.local_pool._healthy.values()  # noqa: SLF001
                            ),
                            claude_available=self.config.claude.enabled,
                        ),
                    )
                    ex.backend = decision.backend
                    ex.routing_reason = decision.reason
                    log.info(
                        "Task %s routed to %s (%s)",
                        task_id,
                        decision.backend.value,
                        decision.reason,
                    )
                    await self._set_status(ex, TaskStatus.CODING)

                    t0 = time.monotonic()
                    worker = (
                        self.local_worker
                        if decision.backend == Backend.LOCAL
                        else self.claude_worker
                    )
                    current_stage = "code"
                    current_timeout = self._stage_timeout(spec, "code")
                    code = await asyncio.wait_for(
                        retry_async(
                            worker.code,
                            spec,
                            ex.plan,
                            sandbox_path,
                            branch,
                            config=self.config.retry,
                            op_name=f"code({task_id})",
                        ),
                        timeout=current_timeout,
                    )

                    sha = await self.sandbox.commit_all(
                        sandbox_path, f"{task_id}: {code.summary or spec.title}"
                    )
                    code.commit_sha = sha
                    ex.code = code
                    await self._set_status(ex, TaskStatus.CODED)
                    await self.state.log_run(
                        task_id,
                        "code",
                        decision.backend.value,
                        True,
                        int((time.monotonic() - t0) * 1000),
                        code.summary,
                    )

            # ---- Review (with iteration on needs_fix) ----------------
            while ex.review_iterations < self.config.orchestrator.review_max_iterations:
                with trace_context(stage="review", review_iter=ex.review_iterations + 1):
                    await self._set_status(ex, TaskStatus.REVIEWING)
                    t0 = time.monotonic()
                    diff = await self.sandbox.diff(sandbox_path)
                    current_stage = "review"
                    current_timeout = self._stage_timeout(spec, "review")
                    review = await asyncio.wait_for(
                        retry_async(
                            self.reviewer.review,
                            spec,
                            ex.plan,
                            ex.code,
                            sandbox_path,
                            diff,
                            config=self.config.retry,
                            op_name=f"review({task_id})",
                        ),
                        timeout=current_timeout,
                    )
                    ex.review = review
                    ex.review_iterations += 1
                    await self.state.log_run(
                        task_id,
                        "review",
                        "claude",
                        review.verdict == ReviewVerdict.APPROVED,
                        int((time.monotonic() - t0) * 1000),
                        f"verdict={review.verdict.value} auto_fixed={review.auto_fixed}",
                    )

                    if review.auto_fixed:
                        # Reviewer already fixed the code; commit fixes
                        sha = await self.sandbox.commit_all(
                            sandbox_path, f"{task_id}: review auto-fix"
                        )
                        if sha:
                            ex.code.commit_sha = sha

                    if review.verdict == ReviewVerdict.APPROVED:
                        break

                    if review.verdict == ReviewVerdict.REJECTED:
                        raise RuntimeError(f"review rejected: {review.issues}")

                    # NEEDS_FIX -> re-plan or have worker redo
                    log.info(
                        "Task %s needs_fix (iter=%d): %s",
                        task_id,
                        ex.review_iterations,
                        review.issues,
                    )
                    if ex.review_iterations >= self.config.orchestrator.review_max_iterations:
                        raise RuntimeError(
                            f"review needs_fix after {ex.review_iterations} iterations"
                        )
                    # Re-run worker with reviewer's issue list appended to plan
                    augmented_plan = ex.plan.model_copy(
                        update={
                            "approach": ex.plan.approach
                            + "\n\n## Reviewer feedback to address\n- "
                            + "\n- ".join(review.issues),
                        }
                    )
                    ex.plan = augmented_plan
                    await self._set_status(ex, TaskStatus.FIXING)
                    worker = (
                        self.local_worker if ex.backend == Backend.LOCAL else self.claude_worker
                    )
                    fixed = await retry_async(
                        worker.code,
                        spec,
                        augmented_plan,
                        sandbox_path,
                        branch,
                        config=self.config.retry,
                        op_name=f"fix({task_id})",
                    )
                    sha = await self.sandbox.commit_all(
                        sandbox_path, f"{task_id}: address reviewer feedback"
                    )
                    fixed.commit_sha = sha
                    ex.code = fixed

            # ---- Done -------------------------------------------------
            ex.finished_at = datetime.now(timezone.utc)
            await self._set_status(ex, TaskStatus.DONE)
            if self.config.sandbox.cleanup_on_success:
                await self.sandbox.cleanup(sandbox_path, branch)
            log.info("Task %s DONE on branch %s", task_id, branch)

        except asyncio.CancelledError:
            ex.last_error = "cancelled"
            await self.state.save_execution(ex)
            raise
        except asyncio.TimeoutError:
            ex.last_error = (
                f"timeout in stage={current_stage} after {current_timeout}s "
                f"(complexity={spec.complexity.value})"
            )
            log.warning("Task %s timed out: %s", task_id, ex.last_error)
            await self._fail(ex, recoverable=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Task %s failed: %s", task_id, exc)
            ex.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            await self._fail(ex, recoverable=True)
            if self.config.orchestrator.fail_fast:
                self._shutdown.set()

    async def _fail(self, ex: TaskExecution, *, recoverable: bool) -> None:
        # Recoverable failures stay READY for a retry up to retry config;
        # but we don't retry the *whole pipeline* here automatically — the
        # task is marked FAILED. Resume can re-enqueue via CLI flag.
        ex.finished_at = datetime.now(timezone.utc)
        await self._set_status(ex, TaskStatus.FAILED)

    async def _set_status(self, ex: TaskExecution, status: TaskStatus) -> None:
        ex.status = status
        ex.touch()
        await self.state.save_execution(ex)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows doesn't support add_signal_handler for SIGTERM
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._request_shutdown, sig)

    def _request_shutdown(self, sig) -> None:
        log.warning("Signal %s received; draining…", sig)
        self._shutdown.set()
        for t in self._running_tasks:
            t.cancel()

    def _summary(self) -> None:
        # Count only tasks that are part of THIS run's spec list.
        # `state.status_counts()` would include leftover rows from previous
        # runs whose task ids no longer appear in tasks.md.
        counts: dict[str, int] = {}
        for ex in self.executions.values():
            counts[ex.status.value] = counts.get(ex.status.value, 0) + 1
        log.info("=== Run summary ===")
        log.info("  total      %d", len(self.executions))
        for status, n in sorted(counts.items()):
            log.info("  %-10s %d", status, n)
