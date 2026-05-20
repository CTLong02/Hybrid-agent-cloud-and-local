"""Config tab.

Two panes:
  - Raw YAML editor for full control.
  - A small form for the handful of fields users tweak most: routing default
    backend, max concurrent tasks, claude model overrides, cost caps. Saving
    the form patches those fields and re-renders the YAML.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Switch, TextArea

from ..config import AppConfig


class ConfigScreen(Widget):
    def __init__(
        self,
        config: AppConfig,
        config_path: Path,
        on_save: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.on_save = on_save

    def compose(self) -> ComposeResult:
        with Horizontal(classes="panel"):
            with Vertical():
                yield Label("[b]Common settings[/b] (form)")
                yield from self._form_rows()
                with Horizontal(classes="form-row"):
                    yield Button("Apply form → YAML", id="apply", variant="primary")
                    yield Button("Reload from disk", id="reload")
            with Vertical():
                yield Label(f"[b]{self.config_path.name}[/b] (raw YAML)")
                yield TextArea(self._yaml_text(), language="yaml", id="yaml-text")
                with Horizontal(classes="form-row"):
                    yield Button("Save YAML to disk (Ctrl+S)", id="save", variant="success")

    def _yaml_text(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def _form_rows(self) -> ComposeResult:
        c = self.config
        with Horizontal(classes="form-row"):
            yield Label("project_root", classes="row-label")
            yield Input(value=c.project_root, id="cf-project_root")
        with Horizontal(classes="form-row"):
            yield Label("routing.default_backend", classes="row-label")
            yield Input(value=c.routing.default_backend, id="cf-default_backend")
        with Horizontal(classes="form-row"):
            yield Label("orch.max_concurrent_tasks", classes="row-label")
            yield Input(value=str(c.orchestrator.max_concurrent_tasks), id="cf-max_concurrent")
        with Horizontal(classes="form-row"):
            yield Label("orch.run_tests_in_review", classes="row-label")
            yield Switch(value=c.orchestrator.run_tests_in_review, id="cf-run_tests")
        with Horizontal(classes="form-row"):
            yield Label("orch.require_tests", classes="row-label")
            yield Switch(value=c.orchestrator.require_tests, id="cf-require_tests")
        with Horizontal(classes="form-row"):
            yield Label("claude.planner_model", classes="row-label")
            yield Input(value=c.claude.planner_model or "", id="cf-planner_model")
        with Horizontal(classes="form-row"):
            yield Label("claude.reviewer_model", classes="row-label")
            yield Input(value=c.claude.reviewer_model or "", id="cf-reviewer_model")
        with Horizontal(classes="form-row"):
            yield Label("claude.coder_model", classes="row-label")
            yield Input(value=c.claude.coder_model or "", id="cf-coder_model")
        with Horizontal(classes="form-row"):
            yield Label("claude.task_generator_model", classes="row-label")
            yield Input(
                value=c.claude.task_generator_model or "",
                id="cf-task_generator_model",
            )
        with Horizontal(classes="form-row"):
            yield Label("cost.per_task_usd_cap", classes="row-label")
            yield Input(value=_opt_str(c.cost.per_task_usd_cap), id="cf-per_task_cap")
        with Horizontal(classes="form-row"):
            yield Label("cost.per_run_usd_cap", classes="row-label")
            yield Input(value=_opt_str(c.cost.per_run_usd_cap), id="cf-per_run_cap")

    # -- actions ---------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.on_save(self.query_one("#yaml-text", TextArea).text)
        elif event.button.id == "reload":
            self.query_one("#yaml-text", TextArea).text = self._yaml_text()
            self.app.notify("Reloaded from disk.")
        elif event.button.id == "apply":
            self._apply_form_to_yaml()

    def _apply_form_to_yaml(self) -> None:
        try:
            data = yaml.safe_load(self.query_one("#yaml-text", TextArea).text) or {}
        except yaml.YAMLError as exc:
            self.app.notify(f"YAML parse failed: {exc}", severity="error")
            return

        def _set(d: dict, path: list[str], value):
            for k in path[:-1]:
                d = d.setdefault(k, {})
            d[path[-1]] = value

        try:
            _set(data, ["project_root"], self._inp("cf-project_root"))
            _set(data, ["routing", "default_backend"], self._inp("cf-default_backend"))
            _set(
                data,
                ["orchestrator", "max_concurrent_tasks"],
                int(self._inp("cf-max_concurrent") or "1"),
            )
            _set(
                data,
                ["orchestrator", "run_tests_in_review"],
                self.query_one("#cf-run_tests", Switch).value,
            )
            _set(
                data,
                ["orchestrator", "require_tests"],
                self.query_one("#cf-require_tests", Switch).value,
            )
            _set(data, ["claude", "planner_model"], self._inp("cf-planner_model") or None)
            _set(data, ["claude", "reviewer_model"], self._inp("cf-reviewer_model") or None)
            _set(data, ["claude", "coder_model"], self._inp("cf-coder_model") or None)
            _set(
                data,
                ["claude", "task_generator_model"],
                self._inp("cf-task_generator_model") or None,
            )
            _set(data, ["cost", "per_task_usd_cap"], _parse_optional_float(self._inp("cf-per_task_cap")))
            _set(data, ["cost", "per_run_usd_cap"], _parse_optional_float(self._inp("cf-per_run_cap")))
        except ValueError as exc:
            self.app.notify(f"Invalid value: {exc}", severity="error")
            return

        new_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        self.query_one("#yaml-text", TextArea).text = new_yaml
        self.app.notify("Form applied to YAML. Click 'Save YAML to disk' to persist.")

    def _inp(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()


def _opt_str(value: float | None) -> str:
    return "" if value is None else str(value)


def _parse_optional_float(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"expected number, got {s!r}") from exc
