"""Tests for `hybrid_agent.llm.claude.ClaudeClient`.

The real claude-agent-sdk spawns the local `claude` CLI as a subprocess; we
can't ship that in CI. Tests inject a fake `claude_agent_sdk` module before
the client imports it.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from hybrid_agent.config import ClaudeConfig, CostConfig
from hybrid_agent.cost import CostMeter, cost_context
from hybrid_agent.llm.claude import ClaudeClient, _extract_text, _extract_usage
from hybrid_agent.retry import FatalLLMError, RetriableLLMError


# ---------------------------------------------------------------------------
# _extract_text — defensive against SDK shape drift
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text: str | None = None):
        self.text = text


class _Msg:
    def __init__(self, content=None, result=None, usage=None):
        if content is not None:
            self.content = content
        if result is not None:
            self.result = result
        if usage is not None:
            self.usage = usage


class TestExtractText:
    def test_blocks_with_text_attr(self):
        msg = _Msg(content=[_Block("hello"), _Block("world")])
        assert _extract_text(msg) == "hello\nworld"

    def test_dict_blocks(self):
        msg = {"content": [{"text": "a"}, {"text": "b"}]}
        assert _extract_text(msg) == "a\nb"

    def test_string_content(self):
        msg = _Msg(content="bare string")
        assert _extract_text(msg) == "bare string"

    def test_falls_back_to_result(self):
        msg = _Msg(result="final answer")
        assert _extract_text(msg) == "final answer"

    def test_empty_when_nothing_found(self):
        assert _extract_text(_Msg()) == ""

    def test_skips_blocks_without_text(self):
        msg = _Msg(content=[_Block(None), _Block("x")])
        assert _extract_text(msg) == "x"


# ---------------------------------------------------------------------------
# _extract_usage
# ---------------------------------------------------------------------------

class TestExtractUsage:
    def test_dict_usage(self):
        msg = _Msg(usage={"input_tokens": 100, "output_tokens": 50})
        assert _extract_usage(msg) == (100, 50)

    def test_alternate_key_names(self):
        msg = _Msg(usage={"prompt_tokens": 5, "completion_tokens": 3})
        assert _extract_usage(msg) == (5, 3)

    def test_missing_returns_zero(self):
        assert _extract_usage(_Msg()) == (0, 0)

    def test_attr_object(self):
        usage = types.SimpleNamespace(input_tokens=7, output_tokens=2)
        assert _extract_usage(_Msg(usage=usage)) == (7, 2)

    def test_unparseable_values_return_zero(self):
        msg = _Msg(usage={"input_tokens": "not-a-number"})
        assert _extract_usage(msg) == (0, 0)


# ---------------------------------------------------------------------------
# complete() — fake SDK
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a fake claude_agent_sdk module that yields configurable msgs."""

    state = {"messages": [], "raise_in_query": None}

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def fake_query(*, prompt, options):
        if state["raise_in_query"]:
            raise state["raise_in_query"]
        for m in state["messages"]:
            yield m

    fake_module = types.ModuleType("claude_agent_sdk")
    fake_module.ClaudeAgentOptions = FakeOptions
    fake_module.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)
    return state


class TestComplete:
    async def test_success_path_with_usage(self, fake_sdk):
        fake_sdk["messages"] = [
            _Msg(content=[_Block("hello")], usage={"input_tokens": 20, "output_tokens": 10}),
        ]
        client = ClaudeClient(ClaudeConfig(enabled=True))
        result = await client.complete("sys", "user")
        assert result.text == "hello"
        assert result.prompt_tokens == 20
        assert result.completion_tokens == 10

    async def test_fallback_token_estimate(self, fake_sdk):
        # No usage info -> char/4 estimate
        fake_sdk["messages"] = [_Msg(content=[_Block("a" * 40)])]
        client = ClaudeClient(ClaudeConfig(enabled=True))
        result = await client.complete("sys", "user")
        assert result.prompt_tokens > 0
        assert result.completion_tokens >= 10  # ~40/4

    async def test_empty_response_is_retriable(self, fake_sdk):
        fake_sdk["messages"] = []
        client = ClaudeClient(ClaudeConfig(enabled=True))
        with pytest.raises(RetriableLLMError, match="empty"):
            await client.complete("sys", "user")

    async def test_sdk_exception_is_retriable(self, fake_sdk):
        fake_sdk["raise_in_query"] = RuntimeError("sdk crashed")
        client = ClaudeClient(ClaudeConfig(enabled=True))
        with pytest.raises(RetriableLLMError, match="sdk error"):
            await client.complete("sys", "user")

    async def test_missing_sdk_is_fatal(self, monkeypatch):
        # Ensure no fake SDK is installed
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        client = ClaudeClient(ClaudeConfig(enabled=True))
        with pytest.raises(FatalLLMError, match="claude-agent-sdk"):
            await client.complete("sys", "user")

    async def test_cost_recorded_on_success(self, fake_sdk):
        fake_sdk["messages"] = [
            _Msg(content=[_Block("ok")], usage={"input_tokens": 1000, "output_tokens": 500}),
        ]
        client = ClaudeClient(ClaudeConfig(enabled=True))
        meter = CostMeter(CostConfig())
        with cost_context(meter):
            await client.complete("sys", "user", model="claude-sonnet-4-5")
        # 1000 prompt @ $3 + 500 completion @ $15 = 0.003 + 0.0075 = 0.0105
        assert meter.run_usage().usd > 0
        assert meter.run_usage().calls == 1


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_disabled_is_unhealthy(self):
        client = ClaudeClient(ClaudeConfig(enabled=False))
        assert await client.health() is False

    async def test_healthy_when_probe_responds_ok(self, fake_sdk):
        fake_sdk["messages"] = [_Msg(content=[_Block("OK")])]
        client = ClaudeClient(ClaudeConfig(enabled=True))
        assert await client.health() is True

    async def test_unhealthy_when_probe_raises(self, fake_sdk):
        fake_sdk["raise_in_query"] = RuntimeError("down")
        client = ClaudeClient(ClaudeConfig(enabled=True))
        assert await client.health() is False
