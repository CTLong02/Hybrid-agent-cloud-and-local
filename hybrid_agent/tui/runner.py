"""Background runner that drives Orchestrator inside the TUI event loop.

The TUI shares Textual's asyncio loop with the orchestrator: `start()` schedules
`Orchestrator.run()` as a task, `stop()` requests shutdown, and the TUI polls
`status()` for a snapshot. Status is read off the StateStore directly so the UI
stays responsive even when the orchestrator is mid-LLM-call.

We deliberately do NOT call `hybrid_agent.logging_config.setup_logging()` here:
it installs a Rich console handler that writes to stdout, which would corrupt
the Textual screen. Instead we wire our own ring-buffer handler plus the
file/json sinks the user configured.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from ..config import AppConfig
from ..cost import CostMeter
from ..models import TaskExecution, TaskSpec, TaskStatus
from ..orchestrator import Orchestrator
from ..state import StateStore

log = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    """In-memory ring buffer of formatted log lines. Drained by the TUI."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[str]:
        return list(self._lines)

    def clear(self) -> None:
        self._lines.clear()


def _install_tui_logging(cfg: AppConfig) -> LogBuffer:
    """Wire root logger for TUI: ring buffer + optional file sinks, no console."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    buf = LogBuffer()
    root.addHandler(buf)

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    if cfg.log_file:
        fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if cfg.log_json_file:
        jh = logging.FileHandler(cfg.log_json_file, encoding="utf-8")
        jh.setFormatter(fmt)
        root.addHandler(jh)

    # Quiet the same noisy deps the CLI quiets.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    return buf


class TUIRunner:
    """Owns the StateStore + (optional) running Orchestrator for the UI."""

    def __init__(self, config: AppConfig, specs: list[TaskSpec]) -> None:
        self.config = config
        self.specs = specs
        self.state = StateStore(config.state_db_path_resolved())
        self.log_buffer = _install_tui_logging(config)

        self._orchestrator: Orchestrator | None = None
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def reload_specs(self, specs: list[TaskSpec]) -> None:
        if self.is_running:
            raise RuntimeError("Cannot reload tasks while a run is in progress.")
        self.specs = specs

    def start(self) -> None:
        if self.is_running:
            return
        self._last_error = None
        try:
            self._orchestrator = Orchestrator(self.config, self.specs, self.state)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"Failed to construct orchestrator: {exc}"
            log.exception("Orchestrator construction failed")
            return
        self._task = asyncio.create_task(self._run(), name="hybrid-agent-run")

    async def _run(self) -> None:
        assert self._orchestrator is not None
        try:
            await self._orchestrator.run()
        except asyncio.CancelledError:
            log.info("Run cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Run crashed: %s", exc)
            self._last_error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        """Request a graceful shutdown of the running orchestrator."""
        if not self.is_running or self._orchestrator is None:
            return
        try:
            self._orchestrator._request_shutdown(signal.SIGINT)  # noqa: SLF001
        except Exception:
            log.exception("stop(): _request_shutdown failed; falling back to cancel")
            if self._task is not None:
                self._task.cancel()

    # -- snapshots --------------------------------------------------------

    def executions(self) -> dict[str, TaskExecution]:
        return self.state.load_executions()

    def cost_meter(self) -> CostMeter | None:
        return self._orchestrator.cost_meter if self._orchestrator else None

    def last_error(self) -> str | None:
        return self._last_error

    def logs(self) -> list[str]:
        return self.log_buffer.snapshot()

    async def reset_tasks(self, task_ids: Iterable[str]) -> int:
        """Reset selected tasks to READY so the next run picks them up."""
        executions = self.state.load_executions()
        touched = 0
        for tid in task_ids:
            ex = executions.get(tid)
            if ex is None:
                continue
            ex.status = TaskStatus.READY
            ex.last_error = ""
            await self.state.save_execution(ex)
            touched += 1
        return touched

    def close(self) -> None:
        self.stop()
        self.state.close()


def sandbox_path_for(execution: TaskExecution) -> Path | None:
    p = Path(execution.sandbox_path) if execution.sandbox_path else None
    return p if p and p.exists() else None
