"""Tests for `hybrid_agent.llm.ollama.OllamaClient` using respx for HTTP mocking."""
from __future__ import annotations

import httpx
import pytest
import respx

from hybrid_agent.config import LocalEndpointConfig
from hybrid_agent.cost import CostMeter, cost_context
from hybrid_agent.config import CostConfig
from hybrid_agent.llm.ollama import OllamaClient
from hybrid_agent.retry import FatalLLMError, RetriableLLMError


def _config(url: str = "http://ollama-test:11434", model: str = "qwen3-coder:30b") -> LocalEndpointConfig:
    return LocalEndpointConfig(
        name="ollama-test",
        url=url,
        model=model,
        timeout_seconds=10,
        max_tokens=512,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

class TestComplete:
    @respx.mock
    async def test_success(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "hello world"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        )
        client = OllamaClient(_config(url="http://o:11434"))
        result = await client.complete("system", "user")
        assert result.text == "hello world"
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 5
        await client.close()

    @respx.mock
    async def test_429_is_retriable(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(429, text="rate limited")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(RetriableLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_500_is_retriable(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="server error")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(RetriableLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_400_is_fatal(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(400, text="bad json")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(FatalLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_401_is_fatal(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(FatalLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_network_error_is_retriable(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("conn refused")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(RetriableLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_timeout_is_retriable(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(RetriableLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_malformed_response_is_fatal(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )
        client = OllamaClient(_config(url="http://o:11434"))
        with pytest.raises(FatalLLMError):
            await client.complete("s", "u")
        await client.close()

    @respx.mock
    async def test_records_cost_to_active_meter(self):
        respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "x"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )
        )
        client = OllamaClient(_config(url="http://o:11434"))
        meter = CostMeter(CostConfig())
        with cost_context(meter):
            await client.complete("s", "u")
        assert meter.run_usage().calls == 1
        assert meter.run_usage().prompt_tokens == 100
        # Local model = $0
        assert meter.run_usage().usd == 0.0
        await client.close()

    @respx.mock
    async def test_api_key_sent_as_bearer(self):
        route = respx.post("http://o:11434/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        cfg = _config(url="http://o:11434")
        cfg.api_key = "secret-token"
        client = OllamaClient(cfg)
        await client.complete("s", "u")
        sent = route.calls[0].request
        assert sent.headers["Authorization"] == "Bearer secret-token"
        await client.close()


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    @respx.mock
    async def test_healthy_when_tags_endpoint_returns_200(self):
        respx.get("http://o:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        client = OllamaClient(_config(url="http://o:11434"))
        assert await client.health() is True
        await client.close()

    @respx.mock
    async def test_unhealthy_on_error(self):
        respx.get("http://o:11434/api/tags").mock(
            side_effect=httpx.ConnectError("down")
        )
        client = OllamaClient(_config(url="http://o:11434"))
        assert await client.health() is False
        await client.close()
