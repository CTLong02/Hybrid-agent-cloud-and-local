"""Token + USD cost tracking with per-task / per-run budget caps.

A `CostMeter` is bound onto a contextvar for the duration of a run; LLM
clients call `record_cost(...)` after each `complete()` and the meter
charges the right task — task_id is read from the same trace contextvar
that powers structured logging, so callers don't need to thread it.

Pricing is per-1M-tokens, hardcoded for Claude families and free for local
models. `cost.pricing_overrides` in config lets you patch any model.

Budget enforcement: when a cap is hit and `on_exceed=stop`, the meter raises
`BudgetExceededError`, which the retry layer treats as fatal (no retries —
the task fails, the orchestrator stops the run if `fail_fast=true`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .config import CostConfig
from .logging_config import get_trace_field

log = logging.getLogger(__name__)


# Per-1M-tokens, USD. Local models (qwen, llama, etc.) default to $0 because
# they're self-hosted; override via `cost.pricing_overrides` if you want to
# attribute electricity / compute costs to them.
#
# Cloud worker-eligible models live here too — anything you point
# LocalEndpointConfig at (OpenAI/DeepSeek/Together/Groq/Fireworks/Mistral/…)
# gets correctly priced through OllamaClient → record_cost.
DEFAULT_PRICING_PER_1M: dict[str, dict[str, float]] = {
    # ---- Anthropic Claude family (list-priced as of 2025) ----
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    # Claude Code SDK reports model name "claude-code" when not explicit
    "claude-code": {"input": 3.0, "output": 15.0},

    # ---- OpenAI (via OpenAI-compatible endpoint) ----
    "gpt-5":            {"input": 1.25, "output": 10.0},
    "gpt-5-mini":       {"input": 0.25, "output": 2.0},
    "gpt-5-nano":       {"input": 0.05, "output": 0.40},
    "gpt-4.1":          {"input": 2.0,  "output": 8.0},
    "gpt-4.1-mini":     {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano":     {"input": 0.10, "output": 0.40},
    "gpt-4o":           {"input": 2.50, "output": 10.0},
    "gpt-4o-mini":      {"input": 0.15, "output": 0.60},
    "o3":               {"input": 2.0,  "output": 8.0},
    "o3-mini":          {"input": 1.10, "output": 4.40},
    "o4-mini":          {"input": 1.10, "output": 4.40},

    # ---- DeepSeek (api.deepseek.com /v1) ----
    "deepseek-chat":     {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-coder":    {"input": 0.27, "output": 1.10},

    # ---- Mistral La Plateforme ----
    "mistral-large-latest":  {"input": 2.0, "output": 6.0},
    "mistral-medium-latest": {"input": 0.4, "output": 2.0},
    "mistral-small-latest":  {"input": 0.2, "output": 0.6},
    "codestral-latest":      {"input": 0.3, "output": 0.9},

    # ---- Together AI (popular code models) ----
    "qwen2.5-coder-32b-instruct": {"input": 0.80, "output": 0.80},
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": {"input": 0.88, "output": 0.88},

    # ---- Groq (fast inference, free-ish tier) ----
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant":    {"input": 0.05, "output": 0.08},

    # ---- Fireworks AI ----
    "accounts/fireworks/models/qwen3-coder-480b": {"input": 0.45, "output": 1.80},

    # ---- Moonshot Kimi ----
    "kimi-k2-instruct": {"input": 0.60, "output": 2.50},

    # ---- xAI Grok (OpenAI-compatible at api.x.ai) ----
    "grok-4":           {"input": 5.0, "output": 15.0},
    "grok-3":           {"input": 3.0, "output": 15.0},
    "grok-3-mini":      {"input": 0.30, "output": 0.50},

    # ---- OpenRouter passthrough patterns are matched by prefix above ----
}


def _resolve_pricing(model: str, overrides: dict[str, dict[str, float]]) -> dict[str, float]:
    if model in overrides:
        return overrides[model]
    if model in DEFAULT_PRICING_PER_1M:
        return DEFAULT_PRICING_PER_1M[model]
    # Best-effort prefix match for versioned model ids
    for prefix, price in DEFAULT_PRICING_PER_1M.items():
        if model.startswith(prefix):
            return price
    return {"input": 0.0, "output": 0.0}


@dataclass
class Usage:
    """Aggregated token + USD usage. Add via `+=` or `add()`."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.usd += other.usd
        self.calls += other.calls

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "usd": round(self.usd, 6),
            "calls": self.calls,
        }


class BudgetExceededError(Exception):
    """A budget cap was hit. Treated as fatal by the retry layer."""


class CostMeter:
    """Thread-safe accumulator for one run.

    Per-task usage is keyed by task_id (extracted from the trace contextvar
    when the LLM client records). Calls without a task_id still count toward
    the run total — useful for housekeeping calls like health probes.
    """

    def __init__(self, config: CostConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._per_task: dict[str, Usage] = {}
        self._run_total = Usage()
        self._per_model: dict[str, Usage] = {}

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        task_id: str | None = None,
    ) -> Usage:
        pricing = _resolve_pricing(model, self.config.pricing_overrides)
        usd = (
            prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]
        ) / 1_000_000.0
        delta = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usd=usd,
            calls=1,
        )
        with self._lock:
            self._run_total.add(delta)
            self._per_model.setdefault(model, Usage()).add(delta)
            if task_id:
                self._per_task.setdefault(task_id, Usage()).add(delta)
        return delta

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def task_usage(self, task_id: str) -> Usage:
        with self._lock:
            return Usage(**self._per_task.get(task_id, Usage()).__dict__)

    def run_usage(self) -> Usage:
        with self._lock:
            return Usage(**self._run_total.__dict__)

    def by_model(self) -> dict[str, Usage]:
        with self._lock:
            return {m: Usage(**u.__dict__) for m, u in self._per_model.items()}

    def by_task(self) -> dict[str, Usage]:
        with self._lock:
            return {t: Usage(**u.__dict__) for t, u in self._per_task.items()}

    # ------------------------------------------------------------------
    # Budget enforcement
    # ------------------------------------------------------------------

    def check_budget(self, task_id: str | None = None) -> None:
        """Raise BudgetExceededError (or warn) if a cap is exceeded."""
        # Per-task cap
        if task_id and self.config.per_task_usd_cap is not None:
            tu = self._per_task.get(task_id)
            if tu and tu.usd >= self.config.per_task_usd_cap:
                self._handle_exceed(
                    f"task {task_id} cost ${tu.usd:.4f} >= cap ${self.config.per_task_usd_cap:.4f}"
                )

        # Per-run cap
        if self.config.per_run_usd_cap is not None:
            ru = self._run_total
            if ru.usd >= self.config.per_run_usd_cap:
                self._handle_exceed(
                    f"run cost ${ru.usd:.4f} >= cap ${self.config.per_run_usd_cap:.4f}"
                )

    def _handle_exceed(self, message: str) -> None:
        if self.config.on_exceed == "stop":
            log.error("Budget exceeded: %s", message)
            raise BudgetExceededError(message)
        log.warning("Budget warning: %s", message)


# ---------------------------------------------------------------------------
# Context binding so LLM clients can record without explicit threading
# ---------------------------------------------------------------------------

_current_meter: ContextVar[CostMeter | None] = ContextVar("hybrid_agent_cost_meter", default=None)


@contextmanager
def cost_context(meter: CostMeter) -> Iterator[CostMeter]:
    """Bind a CostMeter onto the current async context.

    LLM clients inside this block will charge their usage to the bound meter
    via `record_cost(...)`. Outside the block, `record_cost` is a no-op.
    """
    token = _current_meter.set(meter)
    try:
        yield meter
    finally:
        _current_meter.reset(token)


def get_current_meter() -> CostMeter | None:
    return _current_meter.get()


def record_cost(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Charge an LLM call to the active meter (if any) and check budgets.

    No-op if no meter is bound (e.g. unit tests, ad-hoc scripts).
    Raises BudgetExceededError if a cap was hit and `on_exceed=stop`.
    """
    meter = _current_meter.get()
    if meter is None:
        return
    task_id = get_trace_field("task_id")
    meter.record(model, prompt_tokens, completion_tokens, task_id=task_id)
    meter.check_budget(task_id=task_id)


def format_summary(meter: CostMeter) -> str:
    """Pretty multi-line summary for end-of-run logging."""
    run = meter.run_usage()
    lines = [
        "Cost summary:",
        f"  total: ${run.usd:.4f} "
        f"(prompt={run.prompt_tokens}, completion={run.completion_tokens}, "
        f"calls={run.calls})",
    ]
    by_model = meter.by_model()
    if by_model:
        lines.append("  by model:")
        for model, u in sorted(by_model.items(), key=lambda kv: -kv[1].usd):
            lines.append(
                f"    {model}: ${u.usd:.4f} "
                f"({u.prompt_tokens}+{u.completion_tokens} tok, {u.calls} calls)"
            )
    by_task = meter.by_task()
    if by_task:
        lines.append("  by task:")
        for tid, u in sorted(by_task.items(), key=lambda kv: -kv[1].usd):
            lines.append(
                f"    {tid}: ${u.usd:.4f} "
                f"({u.prompt_tokens}+{u.completion_tokens} tok, {u.calls} calls)"
            )
    return "\n".join(lines)
