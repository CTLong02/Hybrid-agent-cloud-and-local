"""Runs tab: live status grid on the left, log tail on the right."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Label, RichLog

from ..models import TaskStatus
from .runner import TUIRunner

_RUN_COLS = ("id", "status", "stage info", "elapsed", "iters")


class RunsScreen(Widget):
    def __init__(self, runner: TUIRunner) -> None:
        super().__init__()
        self.runner = runner

    def compose(self) -> ComposeResult:
        with Horizontal(classes="panel"):
            with Vertical():
                yield Label(id="runs-header")
                yield DataTable(id="runs-table", cursor_type="row", zebra_stripes=True)
            with Vertical():
                yield Label("Logs (newest at bottom)")
                yield RichLog(id="runs-log", highlight=False, markup=False, wrap=False)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*_RUN_COLS)
        self._last_log_count = 0
        self._refresh()
        self.set_interval(0.5, self._refresh)

    def _refresh(self) -> None:
        self._refresh_table()
        self._refresh_logs()

    def _refresh_table(self) -> None:
        executions = self.runner.executions()
        specs = self.runner.specs

        running = "RUNNING" if self.runner.is_running else "idle"
        err = self.runner.last_error()
        hdr = f"[{running}]  ·  {len(specs)} task(s)"
        if err:
            hdr += f"  ·  last error: {err}"
        self.query_one("#runs-header", Label).update(hdr)

        table = self.query_one(DataTable)
        cur: str | None = None
        if table.row_count > 0 and 0 <= table.cursor_row < table.row_count:
            try:
                cur = str(table.get_cell_at(Coordinate(table.cursor_row, 0)))
            except Exception:
                cur = None

        table.clear()
        now = datetime.now(timezone.utc)
        for spec in specs:
            ex = executions.get(spec.id)
            if ex is None:
                table.add_row(spec.id, "pending", "—", "—", "0", key=spec.id)
                continue
            elapsed = "—"
            if ex.started_at and not ex.finished_at:
                elapsed = _fmt_duration((now - ex.started_at).total_seconds())
            elif ex.started_at and ex.finished_at:
                elapsed = _fmt_duration((ex.finished_at - ex.started_at).total_seconds())
            info = _stage_info(ex)
            iters = f"{ex.attempts}/{ex.review_iterations}"
            table.add_row(
                spec.id,
                ex.status.value,
                info,
                elapsed,
                iters,
                key=spec.id,
            )

        if cur:
            for i in range(table.row_count):
                if str(table.get_cell_at(Coordinate(i, 0))) == cur:
                    table.move_cursor(row=i)
                    break

    def _refresh_logs(self) -> None:
        lines = self.runner.logs()
        new = lines[self._last_log_count:]
        if not new:
            return
        rich_log = self.query_one(RichLog)
        for line in new:
            rich_log.write(line)
        self._last_log_count = len(lines)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _stage_info(ex) -> str:
    s = ex.status
    if s == TaskStatus.PLANNING:
        return "planner: claude"
    if s == TaskStatus.PLANNED:
        return "awaiting coder"
    if s == TaskStatus.CODING:
        return f"coder: {ex.backend.value if ex.backend else '?'}"
    if s == TaskStatus.CODED:
        return "awaiting reviewer"
    if s == TaskStatus.REVIEWING:
        return "reviewer: claude"
    if s == TaskStatus.FIXING:
        return f"fixing iter {ex.review_iterations}"
    if s == TaskStatus.DONE:
        return ex.code.summary[:50] if ex.code else "done"
    if s == TaskStatus.FAILED:
        return ex.last_error[:60] if ex.last_error else "failed"
    if s == TaskStatus.BLOCKED:
        return "upstream failed"
    if s == TaskStatus.READY:
        return "ready"
    return "pending"
