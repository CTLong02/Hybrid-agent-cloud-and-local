"""Tests for `hybrid_agent.config`."""
from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_agent.config import (
    AppConfig,
    ClaudeConfig,
    CostConfig,
    LocalEndpointConfig,
    OrchestratorConfig,
    RetryConfig,
    RoutingConfig,
    SandboxConfig,
)


class TestDefaults:
    def test_app_config_defaults(self):
        cfg = AppConfig()
        assert cfg.project_root == "."
        assert cfg.log_level == "INFO"
        assert cfg.log_json_file is None
        assert cfg.orchestrator.max_concurrent_tasks == 4
        assert cfg.orchestrator.run_tests_in_review is True
        assert cfg.cost.on_exceed == "stop"

    def test_claude_default_allowed_tools(self):
        c = ClaudeConfig()
        assert "Read" in c.allowed_tools
        assert "Edit" in c.allowed_tools
        assert c.concurrency == 1

    def test_local_endpoint_default(self):
        e = LocalEndpointConfig()
        assert e.url.startswith("http://")
        assert e.timeout_seconds > 0
        assert e.api_key is None


class TestYamlLoad:
    def test_minimal_yaml(self, tmp_path: Path):
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("project_root: ./work\nlog_level: DEBUG\n")
        cfg = AppConfig.load(cfg_file)
        assert cfg.project_root == "./work"
        assert cfg.log_level == "DEBUG"
        # nested defaults preserved
        assert cfg.orchestrator.max_concurrent_tasks == 4

    def test_empty_yaml_uses_all_defaults(self, tmp_path: Path):
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("")
        cfg = AppConfig.load(cfg_file)
        assert cfg.project_root == "."

    def test_nested_overrides(self, tmp_path: Path):
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("""
orchestrator:
  max_concurrent_tasks: 8
  run_tests_in_review: false
cost:
  per_run_usd_cap: 100.0
  on_exceed: warn
""")
        cfg = AppConfig.load(cfg_file)
        assert cfg.orchestrator.max_concurrent_tasks == 8
        assert cfg.orchestrator.run_tests_in_review is False
        assert cfg.cost.per_run_usd_cap == 100.0
        assert cfg.cost.on_exceed == "warn"

    def test_invalid_on_exceed_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("cost:\n  on_exceed: nuke-from-orbit\n")
        with pytest.raises(Exception):
            AppConfig.load(cfg_file)


class TestProjectRootPath:
    def test_resolves_to_absolute(self, tmp_path: Path):
        cfg = AppConfig(project_root=str(tmp_path))
        assert cfg.project_root_path().is_absolute()
        assert cfg.project_root_path() == tmp_path.resolve()


class TestStateDbPathResolved:
    """Regression: switching project_root must give a fresh state DB,
    otherwise old task statuses leak across unrelated codebases."""

    def test_relative_path_anchored_to_project_root(self, tmp_path: Path):
        cfg = AppConfig(project_root=str(tmp_path))
        # Default db_path is ".hybrid_agent_state.db" (relative)
        resolved = cfg.state_db_path_resolved()
        assert resolved == (tmp_path.resolve() / ".hybrid_agent_state.db")

    def test_absolute_path_used_as_is(self, tmp_path: Path):
        # Caller may opt in to a shared DB by giving an absolute path
        from hybrid_agent.config import StateConfig
        shared = tmp_path / "shared.db"
        cfg = AppConfig(
            project_root=str(tmp_path / "proj"),
            state=StateConfig(db_path=str(shared)),
        )
        assert cfg.state_db_path_resolved() == shared

    def test_two_project_roots_get_different_dbs(self, tmp_path: Path):
        a = tmp_path / "project-a"
        b = tmp_path / "project-b"
        cfg_a = AppConfig(project_root=str(a))
        cfg_b = AppConfig(project_root=str(b))
        assert cfg_a.state_db_path_resolved() != cfg_b.state_db_path_resolved()


class TestSubModelDefaults:
    def test_retry_defaults(self):
        r = RetryConfig()
        assert r.max_attempts >= 1
        assert r.exponential_base >= 1.0

    def test_routing_defaults(self):
        r = RoutingConfig()
        assert r.default_backend in ("local", "claude")
        assert r.escalate_to_claude_after_failures >= 1

    def test_sandbox_defaults(self):
        s = SandboxConfig()
        assert s.branch_prefix.endswith("/")

    def test_cost_defaults(self):
        c = CostConfig()
        assert c.per_task_usd_cap is None  # no cap by default
        assert c.per_run_usd_cap is None
        assert c.pricing_overrides == {}

    def test_orchestrator_test_command_default_is_none(self):
        # null means auto-detect at runtime
        assert OrchestratorConfig().test_command is None
