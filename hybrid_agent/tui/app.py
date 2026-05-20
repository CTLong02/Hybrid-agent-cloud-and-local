"""Textual app shell: header, tab content, footer, global keybindings."""

from __future__ import annotations

from pathlib import Path

import yaml
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Header, TabbedContent, TabPane

from ..config import AppConfig
from .config_screen import ConfigScreen
from .cost_screen import CostScreen
from .logs_screen import LogsScreen
from .runner import TUIRunner
from .runs_screen import RunsScreen
from .tasks_io import load_tasks
from .tasks_screen import TasksScreen


class HybridAgentApp(App):
    """Top-level TUI."""

    CSS = """
    Screen { layout: vertical; }
    #tabs { height: 1fr; }
    .panel { padding: 1 2; }
    .status-pill { padding: 0 1; }
    .pill-pending { background: $panel-darken-1; color: $text; }
    .pill-ready { background: $accent-darken-1; color: $text; }
    .pill-planning,
    .pill-coding,
    .pill-reviewing,
    .pill-fixing { background: $warning; color: $background; }
    .pill-planned,
    .pill-coded { background: $accent; color: $background; }
    .pill-done { background: $success; color: $background; }
    .pill-failed,
    .pill-blocked { background: $error; color: $background; }

    DataTable { height: 1fr; }
    .form-row { height: 3; }
    Input { width: 100%; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "show_tab('tasks')", "Tasks"),
        Binding("r", "show_tab('runs')", "Runs"),
        Binding("c", "show_tab('config')", "Config"),
        Binding("m", "show_tab('cost')", "Cost"),
        Binding("l", "show_tab('logs')", "Logs"),
        Binding("ctrl+r", "run_all", "Run all", show=False),
        Binding("ctrl+s", "stop_run", "Stop", show=False),
    ]

    def __init__(self, *, config_path: Path, tasks_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.tasks_path = tasks_path
        self.config = AppConfig.load(config_path)
        specs = load_tasks(tasks_path)
        self.runner = TUIRunner(self.config, specs)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="tabs"):
            with TabbedContent(initial="tasks"):
                with TabPane("Tasks", id="tasks"):
                    yield TasksScreen(self.runner, self.tasks_path)
                with TabPane("Runs", id="runs"):
                    yield RunsScreen(self.runner)
                with TabPane("Config", id="config"):
                    yield ConfigScreen(self.config, self.config_path, self._reload_config)
                with TabPane("Cost", id="cost"):
                    yield CostScreen(self.runner)
                with TabPane("Logs", id="logs"):
                    yield LogsScreen(self.runner)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Hybrid Agent"
        self.sub_title = f"{self.tasks_path.name}  ·  {self.config_path.name}"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_run_all(self) -> None:
        if self.runner.is_running:
            self.notify("Already running.", severity="warning")
            return
        self.runner.start()
        self.notify("Run started.")
        self.query_one(TabbedContent).active = "runs"

    def action_stop_run(self) -> None:
        if not self.runner.is_running:
            self.notify("Nothing to stop.", severity="warning")
            return
        self.runner.stop()
        self.notify("Stop requested; draining…")

    # ------------------------------------------------------------------
    # Callbacks from child screens
    # ------------------------------------------------------------------

    def _reload_config(self, new_yaml_text: str) -> None:
        """Persist a config edit and rebuild the in-memory config.

        We don't hot-swap the running orchestrator — config changes apply on
        the next run. The Config screen disables Save while a run is active.
        """
        try:
            data = yaml.safe_load(new_yaml_text) or {}
            new_cfg = AppConfig(**data)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Config invalid: {exc}", severity="error", timeout=8)
            return
        self.config_path.write_text(new_yaml_text, encoding="utf-8")
        self.config = new_cfg
        self.notify("Config saved. Takes effect on next run.")

    async def on_unmount(self) -> None:
        self.runner.close()
