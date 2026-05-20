"""Cost tab: live token + USD aggregates from the active CostMeter."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Label, ProgressBar

from ..cost import CostMeter
from .runner import TUIRunner


class CostScreen(Widget):
    def __init__(self, runner: TUIRunner) -> None:
        super().__init__()
        self.runner = runner

    def compose(self) -> ComposeResult:
        with Vertical(classes="panel"):
            yield Label(id="cost-summary")
            yield Label("Budget caps")
            yield ProgressBar(id="cost-bar-task", total=100.0, show_eta=False)
            yield ProgressBar(id="cost-bar-run", total=100.0, show_eta=False)
            yield Label("By model")
            yield DataTable(id="cost-by-model", cursor_type="row", zebra_stripes=True)
            yield Label("By task")
            yield DataTable(id="cost-by-task", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#cost-by-model", DataTable).add_columns(
            "model", "calls", "prompt_tok", "completion_tok", "$"
        )
        self.query_one("#cost-by-task", DataTable).add_columns(
            "task", "calls", "prompt_tok", "completion_tok", "$"
        )
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        meter = self.runner.cost_meter()
        if meter is None:
            self.query_one("#cost-summary", Label).update(
                "[dim]No active run. Cost data appears once you start a run.[/dim]"
            )
            for tbl_id in ("#cost-by-model", "#cost-by-task"):
                self.query_one(tbl_id, DataTable).clear()
            self.query_one("#cost-bar-task", ProgressBar).update(progress=0)
            self.query_one("#cost-bar-run", ProgressBar).update(progress=0)
            return

        run = meter.run_usage()
        self.query_one("#cost-summary", Label).update(
            f"[b]Run total:[/b] ${run.usd:.4f}   "
            f"prompt={run.prompt_tokens:,}   completion={run.completion_tokens:,}   "
            f"calls={run.calls}"
        )

        self._render_caps(meter)
        self._render_by_model(meter)
        self._render_by_task(meter)

    def _render_caps(self, meter: CostMeter) -> None:
        run = meter.run_usage()
        task_cap = meter.config.per_task_usd_cap
        run_cap = meter.config.per_run_usd_cap

        # Per-task: show the most expensive in-progress task vs cap.
        by_task = meter.by_task()
        worst = max(by_task.values(), key=lambda u: u.usd, default=None)
        worst_usd = worst.usd if worst else 0.0
        if task_cap:
            self.query_one("#cost-bar-task", ProgressBar).update(
                total=task_cap, progress=min(worst_usd, task_cap)
            )
        else:
            self.query_one("#cost-bar-task", ProgressBar).update(total=1.0, progress=0)

        if run_cap:
            self.query_one("#cost-bar-run", ProgressBar).update(
                total=run_cap, progress=min(run.usd, run_cap)
            )
        else:
            self.query_one("#cost-bar-run", ProgressBar).update(total=1.0, progress=0)

    def _render_by_model(self, meter: CostMeter) -> None:
        table = self.query_one("#cost-by-model", DataTable)
        table.clear()
        for model, u in sorted(meter.by_model().items(), key=lambda kv: -kv[1].usd):
            table.add_row(
                model,
                str(u.calls),
                f"{u.prompt_tokens:,}",
                f"{u.completion_tokens:,}",
                f"${u.usd:.4f}",
            )

    def _render_by_task(self, meter: CostMeter) -> None:
        table = self.query_one("#cost-by-task", DataTable)
        cur: str | None = None
        if table.row_count > 0 and 0 <= table.cursor_row < table.row_count:
            try:
                cur = str(table.get_cell_at(Coordinate(table.cursor_row, 0)))
            except Exception:
                cur = None
        table.clear()
        for tid, u in sorted(meter.by_task().items(), key=lambda kv: -kv[1].usd):
            table.add_row(
                tid,
                str(u.calls),
                f"{u.prompt_tokens:,}",
                f"{u.completion_tokens:,}",
                f"${u.usd:.4f}",
                key=tid,
            )
        if cur:
            for i in range(table.row_count):
                if str(table.get_cell_at(Coordinate(i, 0))) == cur:
                    table.move_cursor(row=i)
                    break
