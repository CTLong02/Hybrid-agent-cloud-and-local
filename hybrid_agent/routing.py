"""Routing: decide which backend (local vs Claude) handles a task.

Rules are pluggable: each rule sees the task spec, current execution state,
and a routing context (e.g. local pool health). The first rule that returns
a Backend wins; otherwise we fall through to the configured default.

This separation keeps routing testable and lets ops add custom rules without
touching the orchestrator.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Protocol

from .config import RoutingConfig
from .models import Backend, Complexity, TaskExecution, TaskSpec

log = logging.getLogger(__name__)


@dataclass
class RoutingContext:
    """Live signals the router can use beyond the task spec itself."""

    local_available: bool = True  # any local endpoint healthy
    claude_available: bool = True  # claude SDK enabled & healthy
    local_queue_depth: int = 0
    claude_queue_depth: int = 0


@dataclass
class RoutingDecision:
    backend: Backend
    reason: str


class RoutingRule(Protocol):
    name: str

    def evaluate(
        self,
        task: TaskSpec,
        execution: TaskExecution,
        ctx: RoutingContext,
    ) -> Backend | None: ...


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------


class AvailabilityRule:
    """If one backend is down, route everything to the other."""

    name = "availability"

    def evaluate(self, task, execution, ctx):
        if not ctx.local_available and ctx.claude_available:
            return Backend.CLAUDE
        if not ctx.claude_available and ctx.local_available:
            return Backend.LOCAL
        return None


class ForceTagRule:
    """Tags like 'security', 'claude_only', 'local_only'."""

    name = "force_tag"

    def __init__(self, force_claude: list[str], force_local: list[str]) -> None:
        self.force_claude = set(force_claude)
        self.force_local = set(force_local)

    def evaluate(self, task, execution, ctx):
        tag_set = set(task.tags)
        if tag_set & self.force_claude:
            return Backend.CLAUDE
        if tag_set & self.force_local:
            return Backend.LOCAL
        return None


class PathGlobRule:
    """If task targets a sensitive path, route to Claude."""

    name = "path_glob"

    def __init__(self, claude_globs: list[str]) -> None:
        self.claude_globs = list(claude_globs)

    def evaluate(self, task, execution, ctx):
        if not self.claude_globs:
            return None
        for f in task.target_files:
            for pat in self.claude_globs:
                if fnmatch.fnmatch(f, pat):
                    return Backend.CLAUDE
        return None


class ComplexityRule:
    """High-complexity tasks → Claude."""

    name = "complexity"

    def __init__(self, claude_for: list[str]) -> None:
        self.claude_for = {Complexity(c) for c in claude_for}

    def evaluate(self, task, execution, ctx):
        if task.complexity in self.claude_for:
            return Backend.CLAUDE
        return None


class FailureEscalationRule:
    """After N failed attempts on local, escalate to Claude."""

    name = "failure_escalation"

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold

    def evaluate(self, task, execution, ctx):
        if execution.backend == Backend.LOCAL and execution.attempts >= self.threshold:
            return Backend.CLAUDE
        return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    def __init__(self, rules: list[RoutingRule], default: Backend) -> None:
        self.rules = rules
        self.default = default

    def route(
        self,
        task: TaskSpec,
        execution: TaskExecution,
        ctx: RoutingContext,
    ) -> RoutingDecision:
        for rule in self.rules:
            decision = rule.evaluate(task, execution, ctx)
            if decision is not None:
                log.debug("Task %s -> %s by rule %s", task.id, decision.value, rule.name)
                return RoutingDecision(decision, f"rule:{rule.name}")
        return RoutingDecision(self.default, "default")


def build_default_router(cfg: RoutingConfig) -> Router:
    """Wire up the standard rule chain. Order matters."""
    rules: list[RoutingRule] = [
        AvailabilityRule(),
        ForceTagRule(cfg.force_claude_tags, cfg.force_local_tags),
        FailureEscalationRule(cfg.escalate_to_claude_after_failures),
        PathGlobRule(cfg.claude_path_globs),
        ComplexityRule(cfg.claude_for_complexity),
    ]
    return Router(rules, Backend(cfg.default_backend))
