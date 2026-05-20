"""Tasks tab: list, run, reset, edit, add, delete."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, Static, TextArea

from ..models import Complexity, TaskSpec
from .runner import TUIRunner
from .tasks_io import dump_tasks, load_tasks, make_blank, next_task_id

_COLS = ("id", "title", "status", "backend", "attempts", "deps")


class TasksScreen(Widget):
    """List of TaskSpecs with status from StateStore overlaid."""

    BINDINGS = [
        Binding("R", "run_all", "Run all"),
        Binding("s", "stop", "Stop"),
        Binding("x", "reset", "Reset selected"),
        Binding("n", "new_task", "New"),
        Binding("e", "edit_task", "Edit"),
        Binding("d", "delete_task", "Delete"),
        Binding("enter", "show_detail", "Detail"),
    ]

    def __init__(self, runner: TUIRunner, tasks_path: Path) -> None:
        super().__init__()
        self.runner = runner
        self.tasks_path = tasks_path

    def compose(self) -> ComposeResult:
        with Vertical(classes="panel"):
            yield Label(id="tasks-summary")
            yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="form-row"):
                yield Button("Run all (R)", id="btn-run", variant="primary")
                yield Button("Stop (s)", id="btn-stop", variant="warning")
                yield Button("Reset (x)", id="btn-reset")
                yield Button("New (n)", id="btn-new")
                yield Button("Edit (e)", id="btn-edit")
                yield Button("Delete (d)", id="btn-delete", variant="error")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*_COLS)
        self._refresh()
        self.set_interval(1.0, self._refresh)

    # -- refresh ---------------------------------------------------------

    def _refresh(self) -> None:
        table = self.query_one(DataTable)
        executions = self.runner.executions()
        specs = self.runner.specs

        # Preserve cursor row id across rebuilds.
        cur_id: str | None = None
        if table.row_count > 0 and table.cursor_row < table.row_count:
            try:
                cur_id = str(table.get_cell_at(Coordinate(table.cursor_row, 0)))
            except Exception:
                cur_id = None

        table.clear()
        for spec in specs:
            ex = executions.get(spec.id)
            status = ex.status.value if ex else "pending"
            backend = (ex.backend.value if ex and ex.backend else "—")
            attempts = str(ex.attempts) if ex else "0"
            deps = ", ".join(spec.depends_on) or "—"
            table.add_row(
                spec.id,
                _trunc(spec.title, 40),
                _pill(status),
                backend,
                attempts,
                _trunc(deps, 20),
                key=spec.id,
            )

        # Restore selection if possible.
        if cur_id:
            for i in range(table.row_count):
                if str(table.get_cell_at(Coordinate(i, 0))) == cur_id:
                    table.move_cursor(row=i)
                    break

        total = len(specs)
        done = sum(1 for e in executions.values() if e.status.value == "done")
        failed = sum(1 for e in executions.values() if e.status.value in ("failed", "blocked"))
        running = "RUNNING" if self.runner.is_running else "idle"
        self.query_one("#tasks-summary", Label).update(
            f"{total} task(s)  ·  {done} done  ·  {failed} failed/blocked  ·  [{running}]"
        )

    # -- selection helpers ----------------------------------------------

    def _selected_id(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.get_cell_at(Coordinate(table.cursor_row, 0)))
        except Exception:
            return None

    # -- actions --------------------------------------------------------

    def action_run_all(self) -> None:
        self.app.action_run_all()

    def action_stop(self) -> None:
        self.app.action_stop_run()

    async def action_reset(self) -> None:
        tid = self._selected_id()
        if not tid:
            return
        n = await self.runner.reset_tasks([tid])
        self.app.notify(f"Reset {n} task(s).")
        self._refresh()

    def action_new_task(self) -> None:
        if self.runner.is_running:
            self.app.notify("Cannot edit tasks during a run.", severity="warning")
            return
        spec = make_blank(next_task_id(self.runner.specs))
        self.app.push_screen(TaskEditModal(spec, is_new=True), self._on_task_saved)

    def action_edit_task(self) -> None:
        if self.runner.is_running:
            self.app.notify("Cannot edit tasks during a run.", severity="warning")
            return
        tid = self._selected_id()
        if not tid:
            return
        spec = next((s for s in self.runner.specs if s.id == tid), None)
        if spec is None:
            return
        self.app.push_screen(TaskEditModal(spec, is_new=False), self._on_task_saved)

    def action_delete_task(self) -> None:
        if self.runner.is_running:
            self.app.notify("Cannot edit tasks during a run.", severity="warning")
            return
        tid = self._selected_id()
        if not tid:
            return
        self.runner.specs = [s for s in self.runner.specs if s.id != tid]
        dump_tasks(self.tasks_path, self.runner.specs)
        self.app.notify(f"Deleted {tid}.")
        self._refresh()

    def action_show_detail(self) -> None:
        tid = self._selected_id()
        if not tid:
            return
        from .task_detail import TaskDetailModal

        self.app.push_screen(TaskDetailModal(self.runner, tid))

    def _on_task_saved(self, result: TaskSpec | None) -> None:
        if result is None:
            return
        existing = {s.id: i for i, s in enumerate(self.runner.specs)}
        if result.id in existing:
            self.runner.specs[existing[result.id]] = result
        else:
            self.runner.specs.append(result)
        dump_tasks(self.tasks_path, self.runner.specs)
        # Reload from disk to guarantee parser symmetry.
        self.runner.reload_specs(load_tasks(self.tasks_path))
        self.app.notify(f"Saved {result.id}.")
        self._refresh()

    # -- buttons --------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action_map = {
            "btn-run": self.action_run_all,
            "btn-stop": self.action_stop,
            "btn-reset": self.action_reset,
            "btn-new": self.action_new_task,
            "btn-edit": self.action_edit_task,
            "btn-delete": self.action_delete_task,
        }
        fn = action_map.get(event.button.id or "")
        if fn:
            fn()


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _pill(status: str) -> str:
    # Status column is plain text; coloring is done at the row level via CSS
    # classes — DataTable doesn't support per-cell classes without renderables.
    # Keep it as raw text and rely on the user reading the value. Future:
    # use Rich Text with style based on status.
    return status


# ---------------------------------------------------------------------------
# Edit modal
# ---------------------------------------------------------------------------


class TaskEditModal(ModalScreen[TaskSpec | None]):
    DEFAULT_CSS = """
    TaskEditModal { align: center middle; }
    #edit-box { width: 80%; height: 80%; background: $panel; padding: 1 2; }
    TextArea { height: 10; }
    .row-label { width: 18; }
    .field-row { height: 3; layout: horizontal; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, spec: TaskSpec, *, is_new: bool) -> None:
        super().__init__()
        self.spec = spec
        self.is_new = is_new

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Label(f"[bold]{'New' if self.is_new else 'Edit'} task[/bold]")
            with Horizontal(classes="field-row"):
                yield Label("id", classes="row-label")
                yield Input(value=self.spec.id, id="f-id", disabled=not self.is_new)
            with Horizontal(classes="field-row"):
                yield Label("title", classes="row-label")
                yield Input(value=self.spec.title, id="f-title")
            with Horizontal(classes="field-row"):
                yield Label("depends_on", classes="row-label")
                yield Input(value=", ".join(self.spec.depends_on), id="f-deps")
            with Horizontal(classes="field-row"):
                yield Label("tags", classes="row-label")
                yield Input(value=", ".join(self.spec.tags), id="f-tags")
            with Horizontal(classes="field-row"):
                yield Label("complexity", classes="row-label")
                yield Input(value=self.spec.complexity.value, id="f-cx")
            with Horizontal(classes="field-row"):
                yield Label("files", classes="row-label")
                yield Input(value=", ".join(self.spec.target_files), id="f-files")
            yield Label("acceptance criteria")
            yield TextArea(self.spec.acceptance_criteria, id="f-acceptance")
            yield Label("description")
            yield TextArea(self.spec.description, id="f-description")
            with Horizontal(classes="field-row"):
                yield Button("Save (Ctrl+S)", id="save", variant="primary")
                yield Button("Cancel (Esc)", id="cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        try:
            spec = self._collect()
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.dismiss(spec)

    def _collect(self) -> TaskSpec:
        tid = self.query_one("#f-id", Input).value.strip()
        if not tid:
            raise ValueError("id is required")
        title = self.query_one("#f-title", Input).value.strip() or "(untitled)"
        deps = [x.strip() for x in self.query_one("#f-deps", Input).value.split(",") if x.strip()]
        tags = [x.strip() for x in self.query_one("#f-tags", Input).value.split(",") if x.strip()]
        cx_raw = self.query_one("#f-cx", Input).value.strip().lower() or "medium"
        try:
            cx = Complexity(cx_raw)
        except ValueError as exc:
            raise ValueError(f"complexity must be low/medium/high, got {cx_raw!r}") from exc
        files = [x.strip() for x in self.query_one("#f-files", Input).value.split(",") if x.strip()]
        accept = self.query_one("#f-acceptance", TextArea).text
        desc = self.query_one("#f-description", TextArea).text
        return TaskSpec(
            id=tid,
            title=title,
            description=desc,
            depends_on=deps,
            tags=tags,
            complexity=cx,
            target_files=files,
            acceptance_criteria=accept,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()
