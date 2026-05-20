"""Tests for routing rules and `Router.route`."""
from __future__ import annotations

import pytest

from hybrid_agent.config import RoutingConfig
from hybrid_agent.models import Backend, TaskExecution, TaskStatus
from hybrid_agent.routing import (
    AvailabilityRule,
    ComplexityRule,
    FailureEscalationRule,
    ForceTagRule,
    PathGlobRule,
    Router,
    RoutingContext,
    build_default_router,
)


@pytest.fixture
def ctx_both_up() -> RoutingContext:
    return RoutingContext(local_available=True, claude_available=True)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

class TestAvailabilityRule:
    def test_local_down_routes_to_claude(self, make_spec, make_execution):
        rule = AvailabilityRule()
        ctx = RoutingContext(local_available=False, claude_available=True)
        assert rule.evaluate(make_spec("T"), make_execution("T"), ctx) == Backend.CLAUDE

    def test_claude_down_routes_to_local(self, make_spec, make_execution):
        rule = AvailabilityRule()
        ctx = RoutingContext(local_available=True, claude_available=False)
        assert rule.evaluate(make_spec("T"), make_execution("T"), ctx) == Backend.LOCAL

    def test_both_up_no_decision(self, make_spec, make_execution, ctx_both_up):
        rule = AvailabilityRule()
        assert rule.evaluate(make_spec("T"), make_execution("T"), ctx_both_up) is None

    def test_both_down_no_decision(self, make_spec, make_execution):
        rule = AvailabilityRule()
        ctx = RoutingContext(local_available=False, claude_available=False)
        assert rule.evaluate(make_spec("T"), make_execution("T"), ctx) is None


class TestForceTagRule:
    def test_claude_tag_wins(self, make_spec, make_execution, ctx_both_up):
        rule = ForceTagRule(force_claude=["security"], force_local=[])
        spec = make_spec("T", tags=["security", "api"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) == Backend.CLAUDE

    def test_local_tag_wins(self, make_spec, make_execution, ctx_both_up):
        rule = ForceTagRule(force_claude=[], force_local=["local_only"])
        spec = make_spec("T", tags=["local_only"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) == Backend.LOCAL

    def test_no_matching_tag_returns_none(self, make_spec, make_execution, ctx_both_up):
        rule = ForceTagRule(force_claude=["security"], force_local=[])
        spec = make_spec("T", tags=["api"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) is None

    def test_claude_takes_precedence_when_both_match(self, make_spec, make_execution, ctx_both_up):
        # If a task somehow has both tags, claude wins (checked first in code)
        rule = ForceTagRule(force_claude=["security"], force_local=["local_only"])
        spec = make_spec("T", tags=["security", "local_only"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) == Backend.CLAUDE


class TestPathGlobRule:
    def test_glob_matches_routes_to_claude(self, make_spec, make_execution, ctx_both_up):
        rule = PathGlobRule(["**/auth/**"])
        spec = make_spec("T", files=["src/auth/login.py"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) == Backend.CLAUDE

    def test_no_match_returns_none(self, make_spec, make_execution, ctx_both_up):
        rule = PathGlobRule(["**/auth/**"])
        spec = make_spec("T", files=["src/api/users.py"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) is None

    def test_empty_globs_returns_none(self, make_spec, make_execution, ctx_both_up):
        rule = PathGlobRule([])
        spec = make_spec("T", files=["src/auth/login.py"])
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) is None


class TestComplexityRule:
    def test_high_complexity_routes_to_claude(self, make_spec, make_execution, ctx_both_up):
        rule = ComplexityRule(["high"])
        spec = make_spec("T", complexity="high")
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) == Backend.CLAUDE

    def test_low_complexity_returns_none(self, make_spec, make_execution, ctx_both_up):
        rule = ComplexityRule(["high"])
        spec = make_spec("T", complexity="low")
        assert rule.evaluate(spec, make_execution("T"), ctx_both_up) is None


class TestFailureEscalationRule:
    def test_local_at_threshold_escalates(self, make_spec, make_execution, ctx_both_up):
        rule = FailureEscalationRule(threshold=2)
        ex = make_execution("T", attempts=2, backend=Backend.LOCAL)
        assert rule.evaluate(make_spec("T"), ex, ctx_both_up) == Backend.CLAUDE

    def test_local_below_threshold_no_escalation(self, make_spec, make_execution, ctx_both_up):
        rule = FailureEscalationRule(threshold=2)
        ex = make_execution("T", attempts=1, backend=Backend.LOCAL)
        assert rule.evaluate(make_spec("T"), ex, ctx_both_up) is None

    def test_claude_attempts_dont_escalate(self, make_spec, make_execution, ctx_both_up):
        rule = FailureEscalationRule(threshold=2)
        ex = make_execution("T", attempts=10, backend=Backend.CLAUDE)
        assert rule.evaluate(make_spec("T"), ex, ctx_both_up) is None


# ---------------------------------------------------------------------------
# Router (chain)
# ---------------------------------------------------------------------------

class TestRouter:
    def test_first_matching_rule_wins(self, make_spec, make_execution, ctx_both_up):
        # Force-tag claude triggers first; complexity rule never checked
        router = Router(
            rules=[
                ForceTagRule(force_claude=["security"], force_local=[]),
                ComplexityRule(["high"]),  # would also pick claude
            ],
            default=Backend.LOCAL,
        )
        spec = make_spec("T", tags=["security"], complexity="low")
        decision = router.route(spec, make_execution("T"), ctx_both_up)
        assert decision.backend == Backend.CLAUDE
        assert "force_tag" in decision.reason

    def test_falls_through_to_default(self, make_spec, make_execution, ctx_both_up):
        router = Router(rules=[ForceTagRule(["x"], ["y"])], default=Backend.LOCAL)
        spec = make_spec("T", tags=["unrelated"])
        decision = router.route(spec, make_execution("T"), ctx_both_up)
        assert decision.backend == Backend.LOCAL
        assert decision.reason == "default"

    def test_build_default_router_from_config(self, make_spec, make_execution, ctx_both_up):
        cfg = RoutingConfig(
            default_backend="local",
            force_claude_tags=["security"],
            force_local_tags=[],
            claude_for_complexity=["high"],
            escalate_to_claude_after_failures=2,
            claude_path_globs=["**/auth/**"],
        )
        router = build_default_router(cfg)
        # Security tag → claude
        spec = make_spec("T", tags=["security"])
        assert router.route(spec, make_execution("T"), ctx_both_up).backend == Backend.CLAUDE
        # No match → default
        spec = make_spec("T2", tags=["misc"])
        assert router.route(spec, make_execution("T2"), ctx_both_up).backend == Backend.LOCAL

    def test_availability_overrides_everything(self, make_spec, make_execution):
        """If local is down, even a force_local task must go to Claude."""
        cfg = RoutingConfig(
            default_backend="local",
            force_local_tags=["local_only"],
        )
        router = build_default_router(cfg)
        spec = make_spec("T", tags=["local_only"])
        ctx = RoutingContext(local_available=False, claude_available=True)
        assert router.route(spec, make_execution("T"), ctx).backend == Backend.CLAUDE
