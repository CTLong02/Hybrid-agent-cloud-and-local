"""Modal showing plan + code summary + review verdict + diff for one task."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TabbedContent, TabPane

from .runner import TUIRunner


class TaskDetailModal(ModalScreen):
    DEFAULT_CSS = """
    TaskDetailModal { align: center middle; }
    #detail-box { width: 95%; height: 90%; background: $panel; padding: 1 2; }
    .detail-content { height: 1fr; overflow: auto auto; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, runner: TUIRunner, task_id: str) -> None:
        super().__init__()
        self.runner = runner
        self.task_id = task_id

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-box"):
            yield Label(f"[bold]Task {self.task_id}[/bold]")
            with TabbedContent(initial="overview"):
                with TabPane("Overview", id="overview"):
                    yield Static(self._render_overview(), classes="detail-content")
                with TabPane("Plan", id="plan"):
                    yield Static(self._render_plan(), classes="detail-content")
                with TabPane("Review", id="review"):
                    yield Static(self._render_review(), classes="detail-content")
                with TabPane("Diff", id="diff"):
                    yield Static("Loading diff…", id="diff-pane", classes="detail-content")
            yield Button("Close (Esc)", id="close")

    async def on_mount(self) -> None:
        asyncio.create_task(self._load_diff())

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.action_close()

    # -- renderers -------------------------------------------------------

    def _ex(self):
        return self.runner.executions().get(self.task_id)

    def _spec(self):
        return next((s for s in self.runner.specs if s.id == self.task_id), None)

    def _render_overview(self) -> str:
        spec = self._spec()
        ex = self._ex()
        if spec is None:
            return "(spec not found)"
        lines = [
            f"[b]Title:[/b] {spec.title}",
            f"[b]Complexity:[/b] {spec.complexity.value}",
            f"[b]Tags:[/b] {', '.join(spec.tags) or '—'}",
            f"[b]Depends on:[/b] {', '.join(spec.depends_on) or '—'}",
            f"[b]Target files:[/b] {', '.join(spec.target_files) or '—'}",
            "",
            f"[b]Acceptance:[/b]\n{spec.acceptance_criteria or '(none)'}",
            "",
            f"[b]Description:[/b]\n{spec.description or '(none)'}",
        ]
        if ex:
            lines += [
                "",
                f"[b]Status:[/b] {ex.status.value}",
                f"[b]Backend:[/b] {ex.backend.value if ex.backend else '—'}  "
                f"({ex.routing_reason or 'no routing reason'})",
                f"[b]Attempts:[/b] {ex.attempts}   "
                f"[b]Review iterations:[/b] {ex.review_iterations}",
                f"[b]Sandbox:[/b] {ex.sandbox_path or '—'}  [{ex.sandbox_branch or '—'}]",
            ]
            if ex.last_error:
                lines.append(f"[b red]Last error:[/b red] {ex.last_error}")
        return "\n".join(lines)

    def _render_plan(self) -> str:
        ex = self._ex()
        if ex is None or ex.plan is None:
            return "(no plan yet)"
        p = ex.plan
        return "\n".join(
            [
                f"[b]Approach:[/b]\n{p.approach or '(empty)'}",
                "",
                f"[b]Test strategy:[/b]\n{p.test_strategy or '(empty)'}",
                "",
                f"[b]Estimated LOC:[/b] {p.estimated_loc}",
                f"[b]Files to modify:[/b] {', '.join(p.files_to_modify) or '—'}",
                f"[b]Files to create:[/b] {', '.join(p.files_to_create) or '—'}",
                "",
                "[b]Context snippets:[/b]",
                *(
                    f"\n[u]{path}[/u]\n{snip}"
                    for path, snip in p.context_snippets.items()
                ),
            ]
        )

    def _render_review(self) -> str:
        ex = self._ex()
        if ex is None or ex.review is None:
            return "(no review yet)"
        r = ex.review
        issues = "\n".join(f"  - {i}" for i in r.issues) or "  (none)"
        return "\n".join(
            [
                f"[b]Verdict:[/b] {r.verdict.value}",
                f"[b]Score:[/b] {r.score:.2f}",
                f"[b]Auto-fixed:[/b] {r.auto_fixed}",
                "",
                f"[b]Issues:[/b]\n{issues}",
            ]
        )

    async def _load_diff(self) -> None:
        ex = self._ex()
        pane = self.query_one("#diff-pane", Static)
        if ex is None or not ex.sandbox_path:
            pane.update("(no sandbox)")
            return
        sandbox = Path(ex.sandbox_path)
        if not sandbox.exists():
            pane.update("(sandbox no longer on disk)")
            return
        base = self.runner.config.sandbox.base_branch
        try:
            diff_text = await asyncio.to_thread(_git_diff, sandbox, base)
        except Exception as exc:  # noqa: BLE001
            pane.update(f"diff failed: {exc}")
            return
        if not diff_text.strip():
            pane.update("(no diff)")
            return
        pane.update(Syntax(diff_text[:200_000], "diff", theme="ansi_dark", word_wrap=False))


def _git_diff(sandbox: Path, base_branch: str) -> str:
    result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else (result.stderr or "")
