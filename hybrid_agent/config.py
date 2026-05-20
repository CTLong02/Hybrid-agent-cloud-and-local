"""Application configuration loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class LocalEndpointConfig(BaseModel):
    """A single Ollama (or other OpenAI-compatible) endpoint."""

    name: str = "ollama-default"
    url: str = "http://localhost:11434"
    model: str = "qwen3-coder:30b"
    timeout_seconds: int = 600
    max_tokens: int = 8192
    temperature: float = 0.2
    # Auth (Ollama default = none; some forks require bearer)
    api_key: str | None = None


class LocalConfig(BaseModel):
    """Local LLM pool. Each endpoint can serve one task at a time."""

    endpoints: list[LocalEndpointConfig] = Field(default_factory=lambda: [LocalEndpointConfig()])


class ClaudeConfig(BaseModel):
    """Claude Code SDK configuration. Auth is handled by the CLI (no API key)."""

    enabled: bool = True
    concurrency: int = 1
    cwd: str | None = None  # Defaults to project_root
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]
    )
    max_turns: int = 30
    # Optional model override (Claude Code resolves the actual model)
    planner_model: str | None = None
    reviewer_model: str | None = None
    coder_model: str | None = None
    task_generator_model: str | None = None


class RetryConfig(BaseModel):
    max_attempts: int = 5
    initial_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


class RoutingConfig(BaseModel):
    """Routing rules. Order matters; first match wins."""

    default_backend: str = "local"
    force_claude_tags: list[str] = Field(
        default_factory=lambda: ["security", "auth", "payment", "claude_only"]
    )
    force_local_tags: list[str] = Field(default_factory=lambda: ["local_only"])
    claude_for_complexity: list[str] = Field(default_factory=lambda: ["high"])
    escalate_to_claude_after_failures: int = 2
    # Path globs that always go to Claude
    claude_path_globs: list[str] = Field(default_factory=list)


class SandboxConfig(BaseModel):
    base_dir: str = ".hybrid_agent_sandboxes"
    use_git_worktree: bool = True
    base_branch: str = "main"
    branch_prefix: str = "agent/"
    cleanup_on_success: bool = False


class StateConfig(BaseModel):
    db_path: str = ".hybrid_agent_state.db"


class OrchestratorConfig(BaseModel):
    poll_interval_seconds: float = 0.5
    max_concurrent_tasks: int = 4
    fail_fast: bool = False
    auto_fix_on_review: bool = True
    review_max_iterations: int = 2
    # Per-stage timeouts (base values for complexity=medium)
    plan_timeout_seconds: int = 600
    code_timeout_seconds: int = 1800
    review_timeout_seconds: int = 600
    # Multiplier applied to the base timeouts above, keyed by Complexity value.
    # A high-complexity task with multiplier 2.0 gets twice the time budget.
    # Unknown complexities fall back to 1.0 (no change).
    complexity_timeout_multiplier: dict[str, float] = Field(
        default_factory=lambda: {"low": 0.75, "medium": 1.0, "high": 2.0}
    )

    # ---- Reviewer-runs-tests gate -------------------------------------------
    # When true, the reviewer runs the project's tests after its LLM-driven
    # review. Anything other than PASSED forces NEEDS_FIX (NO_TESTS still
    # passes if `require_tests=false`).
    run_tests_in_review: bool = True
    require_tests: bool = False
    test_timeout_seconds: int = 300
    # null = auto-detect from project marker files (pyproject.toml, go.mod, …).
    # Set explicitly for mixed-language projects.
    test_command: str | None = None

    # ---- merge --review system-test gate ------------------------------------
    # Run after the unit-test gate on the merged tree to verify the project
    # actually works end-to-end. null = auto-detect tests/integration,
    # tests/e2e, tests/system, integration_tests, or e2e/ (with package.json
    # scripts). Set explicitly for custom flows like "docker compose up &&
    # pytest tests/system" or "npm run smoke".
    system_test_command: str | None = None
    # Bigger budget than unit tests — system tests usually spin up servers,
    # hit databases, etc. Independent timeout so a slow system test doesn't
    # require dragging the unit-test budget up.
    system_test_timeout_seconds: int = 900


class CostConfig(BaseModel):
    """Token + USD cost tracking and budget caps.

    Caps are USD spent on paid models (Claude); local LLMs default to $0
    unless `pricing_overrides` says otherwise.
    """

    per_task_usd_cap: float | None = None  # None = no cap
    per_run_usd_cap: float | None = None
    on_exceed: Literal["stop", "warn"] = "stop"
    # model -> {"input": <usd_per_1M>, "output": <usd_per_1M>}
    pricing_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    project_root: str = "."
    log_level: str = "INFO"
    log_file: str | None = None
    log_json_file: str | None = None  # JSON-lines for log aggregation

    state: StateConfig = Field(default_factory=StateConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    cost: CostConfig = Field(default_factory=CostConfig)

    local: LocalConfig = Field(default_factory=LocalConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)

    # Set by `load()`; used as the anchor for resolving relative paths.
    # Not part of the YAML schema — excluded from validation/serialization.
    config_dir: Path | None = Field(default=None, exclude=True, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        cfg_path = Path(path).resolve()
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls(**data)
        cfg.config_dir = cfg_path.parent
        return cfg

    def _anchor(self) -> Path:
        """Where relative paths in this config are resolved against.

        Falls back to CWD if `load()` wasn't used (e.g. constructed in tests).
        Anchoring to the config file's directory makes paths invariant to
        where the CLI / webUI is launched from — invoking from ``hybrid_agent/``
        vs. ``hybrid_agent/my-codebase/`` no longer produces two different
        state DBs.
        """
        return self.config_dir or Path.cwd()

    def project_root_path(self) -> Path:
        p = Path(self.project_root)
        if p.is_absolute():
            return p
        return (self._anchor() / p).resolve()

    def state_db_path_resolved(self) -> Path:
        """Resolve `state.db_path` against `project_root` if it's relative.

        Without this, switching `project_root` between runs would keep
        reading the old project's state DB from the CWD — leaking
        task statuses across unrelated codebases.

        Behaviour:
          - Absolute path in config → used as-is (intentional sharing).
          - Relative path → resolved under `project_root_path()`,
            which makes the DB live alongside the sandbox dir.
        """
        p = Path(self.state.db_path)
        if p.is_absolute():
            return p
        return self.project_root_path() / p
