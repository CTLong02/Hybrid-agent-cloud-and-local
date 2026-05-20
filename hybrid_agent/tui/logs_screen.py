"""Logs tab: tail of the in-memory ring buffer with level filter and follow.

The runner's `LogBuffer` keeps the most recent N formatted log lines (see
`tui/runner.py`). This screen polls `runner.logs()` and appends only new lines
to a `RichLog`. It also offers:

  - level filter (DEBUG / INFO / WARNING / ERROR / CRITICAL)
  - follow toggle: when off, new lines are buffered but auto-scroll pauses so
    the user can read scrollback without it jumping
  - clear: drops both the screen view and the underlying ring buffer
  - save: dumps the current snapshot to a timestamped file

The format produced by `LogBuffer` is::

    HH:MM:SS LEVEL  name: message

`LEVEL` is `%(levelname)-5s` (left-justified to 5 chars), so we can find it at
character offset 9 of each line — cheap, no regex.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Label, RichLog, Select, Switch

from .runner import TUIRunner

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_ORDER = {name: i for i, name in enumerate(_LEVELS)}


class LogsScreen(Widget):
    """Live log tail with level filter, follow toggle, clear, and save."""

    BINDINGS = [
        Binding("f", "toggle_follow", "Follow"),
        Binding("c", "clear_logs", "Clear"),
        Binding("S", "save_logs", "Save"),
    ]

    DEFAULT_CSS = """
    LogsScreen { layout: vertical; }
    LogsScreen .toolbar { height: 3; padding: 0 1; }
    LogsScreen .toolbar Label { width: auto; padding: 0 1; }
    LogsScreen Select { width: 18; }
    LogsScreen RichLog { height: 1fr; }
    """

    def __init__(self, runner: TUIRunner) -> None:
        super().__init__()
        self.runner = runner
        self._seen = 0
        self._follow = True
        self._min_level = "INFO"

    def compose(self) -> ComposeResult:
        with Vertical(classes="panel"):
            with Horizontal(classes="toolbar"):
                yield Label("Level:")
                yield Select(
                    [(lvl, lvl) for lvl in _LEVELS],
                    value=self._min_level,
                    id="log-level",
                    allow_blank=False,
                )
                yield Label("Follow:")
                yield Switch(value=self._follow, id="log-follow")
                yield Button("Clear (c)", id="btn-log-clear")
                yield Button("Save (S)", id="btn-log-save")
                yield Label(id="log-status")
            yield RichLog(id="log-view", highlight=False, markup=False, wrap=False)

    def on_mount(self) -> None:
        self._update_status()
        # Seed with whatever is already in the buffer so the user isn't staring
        # at a blank screen when they switch tabs after a run has been going.
        self._rebuild()
        self.set_interval(0.5, self._tick)

    # -- ticking ---------------------------------------------------------

    def _tick(self) -> None:
        """Append only the new lines since the last tick (cheap path)."""
        lines = self.runner.logs()
        if len(lines) < self._seen:
            # Buffer was cleared or rotated under us — rebuild from scratch.
            self._rebuild()
            return
        new = lines[self._seen:]
        if not new:
            return
        self._seen = len(lines)
        view = self.query_one("#log-view", RichLog)
        threshold = _LEVEL_ORDER[self._min_level]
        for line in new:
            if _level_index(line) < threshold:
                continue
            view.write(line, scroll_end=self._follow)
        self._update_status()

    def _rebuild(self) -> None:
        """Re-render the entire visible buffer (used on filter/level change)."""
        view = self.query_one("#log-view", RichLog)
        view.clear()
        lines = self.runner.logs()
        self._seen = len(lines)
        threshold = _LEVEL_ORDER[self._min_level]
        for line in lines:
            if _level_index(line) < threshold:
                continue
            view.write(line, scroll_end=False)
        if self._follow:
            view.scroll_end(animate=False)
        self._update_status()

    def _update_status(self) -> None:
        total = len(self.runner.logs())
        follow = "follow" if self._follow else "paused"
        self.query_one("#log-status", Label).update(
            f"  {total:,} line(s)  ·  ≥{self._min_level}  ·  {follow}"
        )

    # -- actions ---------------------------------------------------------

    def action_toggle_follow(self) -> None:
        sw = self.query_one("#log-follow", Switch)
        sw.value = not sw.value  # triggers on_switch_changed

    def action_clear_logs(self) -> None:
        self.runner.log_buffer.clear()
        self._rebuild()
        self.app.notify("Logs cleared.")

    def action_save_logs(self) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path.cwd() / f"hybrid_agent-logs-{ts}.log"
        try:
            out.write_text("\n".join(self.runner.logs()) + "\n", encoding="utf-8")
        except OSError as exc:
            self.app.notify(f"Save failed: {exc}", severity="error")
            return
        self.app.notify(f"Saved to {out}")

    # -- events ----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-log-clear":
            self.action_clear_logs()
        elif event.button.id == "btn-log-save":
            self.action_save_logs()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id != "log-follow":
            return
        self._follow = event.value
        if self._follow:
            self.query_one("#log-view", RichLog).scroll_end(animate=False)
        self._update_status()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "log-level":
            return
        value = event.value
        if not isinstance(value, str) or value not in _LEVEL_ORDER:
            return
        self._min_level = value
        self._rebuild()


def _level_index(line: str) -> int:
    """Return the level rank of a formatted log line, or -1 if unparseable.

    LogBuffer format: ``HH:MM:SS LEVEL  name: message``. The level token starts
    at offset 9 and is left-justified to 5 chars by ``%(levelname)-5s``. We
    match against the canonical prefix to handle both 4-char (INFO ) and 5-char
    (DEBUG/ERROR) levels, and the 7-char ``WARNING``/``CRITICAL`` cases where
    the field overflows the 5-char minimum.
    """
    if len(line) < 10:
        return -1
    tail = line[9:]
    for name, idx in _LEVEL_ORDER.items():
        if tail.startswith(name):
            return idx
    return -1
