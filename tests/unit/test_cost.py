"""Tests for `hybrid_agent.cost`."""
from __future__ import annotations

import pytest

from hybrid_agent.config import CostConfig
from hybrid_agent.cost import (
    BudgetExceededError,
    CostMeter,
    Usage,
    _resolve_pricing,
    cost_context,
    format_summary,
    get_current_meter,
    record_cost,
)
from hybrid_agent.logging_config import trace_context


class TestUsage:
    def test_add_accumulates(self):
        u = Usage()
        u.add(Usage(prompt_tokens=10, completion_tokens=5, usd=0.1, calls=1))
        u.add(Usage(prompt_tokens=20, completion_tokens=10, usd=0.2, calls=1))
        assert u.prompt_tokens == 30
        assert u.completion_tokens == 15
        assert u.usd == pytest.approx(0.3)
        assert u.calls == 2

    def test_to_dict_rounds_usd(self):
        u = Usage(usd=0.123456789)
        assert u.to_dict()["usd"] == round(0.123456789, 6)


class TestPricing:
    def test_known_model(self):
        p = _resolve_pricing("claude-sonnet-4-5", {})
        assert p["input"] == 3.0
        assert p["output"] == 15.0

    def test_unknown_model_is_free(self):
        p = _resolve_pricing("qwen3-coder:30b", {})
        assert p["input"] == 0.0
        assert p["output"] == 0.0

    def test_override_wins_over_default(self):
        overrides = {"claude-sonnet-4-5": {"input": 1.0, "output": 1.0}}
        p = _resolve_pricing("claude-sonnet-4-5", overrides)
        assert p == {"input": 1.0, "output": 1.0}

    def test_prefix_match_for_versioned_id(self):
        # Versioned id like "claude-sonnet-4-5-20251001" should match prefix
        p = _resolve_pricing("claude-sonnet-4-5-20251001", {})
        assert p["input"] == 3.0


class TestCostMeter:
    def test_record_paid_model(self):
        m = CostMeter(CostConfig())
        m.record("claude-sonnet-4-5", 10000, 5000, task_id="T1")
        # 10000/1M * $3 + 5000/1M * $15 = 0.03 + 0.075 = 0.105
        assert m.run_usage().usd == pytest.approx(0.105)
        assert m.task_usage("T1").usd == pytest.approx(0.105)

    def test_local_model_is_free(self):
        m = CostMeter(CostConfig())
        m.record("qwen3-coder:30b", 50000, 10000, task_id="T1")
        assert m.run_usage().usd == 0.0
        assert m.run_usage().prompt_tokens == 50000

    def test_per_task_isolation(self):
        m = CostMeter(CostConfig())
        m.record("claude-sonnet-4-5", 1000, 0, task_id="T1")
        m.record("claude-sonnet-4-5", 2000, 0, task_id="T2")
        assert m.task_usage("T1").prompt_tokens == 1000
        assert m.task_usage("T2").prompt_tokens == 2000

    def test_calls_without_task_only_count_run_total(self):
        m = CostMeter(CostConfig())
        m.record("claude-sonnet-4-5", 1000, 0, task_id=None)
        assert m.run_usage().prompt_tokens == 1000
        assert m.by_task() == {}

    def test_by_model_grouping(self):
        m = CostMeter(CostConfig())
        m.record("claude-sonnet-4-5", 1000, 0)
        m.record("claude-haiku-4-5", 1000, 0)
        m.record("claude-sonnet-4-5", 2000, 0)
        by = m.by_model()
        assert by["claude-sonnet-4-5"].calls == 2
        assert by["claude-haiku-4-5"].calls == 1


class TestBudgetEnforcement:
    def test_per_task_cap_raises_in_stop_mode(self):
        cfg = CostConfig(per_task_usd_cap=0.01, on_exceed="stop")
        m = CostMeter(cfg)
        m.record("claude-sonnet-4-5", 10000, 5000, task_id="T1")  # $0.105
        with pytest.raises(BudgetExceededError, match="T1"):
            m.check_budget(task_id="T1")

    def test_per_run_cap_raises(self):
        cfg = CostConfig(per_run_usd_cap=0.01, on_exceed="stop")
        m = CostMeter(cfg)
        m.record("claude-sonnet-4-5", 10000, 0)  # $0.03
        with pytest.raises(BudgetExceededError, match="run"):
            m.check_budget()

    def test_warn_mode_does_not_raise(self, caplog):
        cfg = CostConfig(per_run_usd_cap=0.01, on_exceed="warn")
        m = CostMeter(cfg)
        m.record("claude-sonnet-4-5", 10000, 0)
        # Should not raise
        m.check_budget()

    def test_under_cap_does_not_raise(self):
        cfg = CostConfig(per_task_usd_cap=10.0, per_run_usd_cap=100.0)
        m = CostMeter(cfg)
        m.record("claude-sonnet-4-5", 100, 50, task_id="T1")
        m.check_budget(task_id="T1")  # well under

    def test_no_cap_means_no_check(self):
        cfg = CostConfig()  # both caps None
        m = CostMeter(cfg)
        m.record("claude-sonnet-4-5", 10_000_000, 10_000_000, task_id="T1")
        m.check_budget(task_id="T1")  # no raise


class TestContextvar:
    def test_record_cost_no_op_without_meter(self):
        # No meter bound — should silently do nothing
        record_cost("claude-sonnet-4-5", 1000, 500)
        assert get_current_meter() is None

    def test_record_cost_routes_to_bound_meter(self):
        m = CostMeter(CostConfig())
        with cost_context(m):
            assert get_current_meter() is m
            record_cost("claude-sonnet-4-5", 1000, 500)
        assert m.run_usage().calls == 1

    def test_record_cost_uses_task_id_from_trace_context(self):
        m = CostMeter(CostConfig())
        with cost_context(m), trace_context(task_id="T_X"):
            record_cost("claude-sonnet-4-5", 1000, 500)
        assert m.task_usage("T_X").calls == 1

    def test_meter_unbound_after_context_exits(self):
        m = CostMeter(CostConfig())
        with cost_context(m):
            pass
        assert get_current_meter() is None


class TestFormatSummary:
    def test_summary_includes_totals(self):
        m = CostMeter(CostConfig())
        m.record("claude-sonnet-4-5", 1000, 500, task_id="T1")
        s = format_summary(m)
        assert "total:" in s
        assert "claude-sonnet-4-5" in s
        assert "T1" in s

    def test_empty_meter_shows_zero(self):
        s = format_summary(CostMeter(CostConfig()))
        assert "total:" in s
