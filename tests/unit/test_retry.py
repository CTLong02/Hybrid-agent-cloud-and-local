"""Tests for `hybrid_agent.retry`."""
from __future__ import annotations

import asyncio

import pytest

from hybrid_agent.cost import BudgetExceededError
from hybrid_agent.retry import (
    FatalLLMError,
    RetriableLLMError,
    classify_http_error,
    retry_async,
)


# ---------------------------------------------------------------------------
# classify_http_error
# ---------------------------------------------------------------------------

class TestClassifyHttp:
    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_errors_are_fatal(self, code):
        assert isinstance(classify_http_error(code, ""), FatalLLMError)

    def test_400_is_fatal(self):
        assert isinstance(classify_http_error(400, "bad json"), FatalLLMError)

    def test_404_is_fatal(self):
        assert isinstance(classify_http_error(404, ""), FatalLLMError)

    def test_429_is_retriable(self):
        assert isinstance(classify_http_error(429, ""), RetriableLLMError)

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_5xx_is_retriable(self, code):
        assert isinstance(classify_http_error(code, ""), RetriableLLMError)

    def test_unknown_falls_back_to_retriable(self):
        assert isinstance(classify_http_error(418, ""), RetriableLLMError)


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------

class TestRetryAsync:
    async def test_returns_first_success(self, fast_retry):
        calls = []

        async def f():
            calls.append(1)
            return "ok"

        result = await retry_async(f, config=fast_retry)
        assert result == "ok"
        assert len(calls) == 1

    async def test_retries_on_retriable_error(self, fast_retry):
        calls = []

        async def f():
            calls.append(1)
            if len(calls) < 2:
                raise RetriableLLMError("flake")
            return "ok"

        result = await retry_async(f, config=fast_retry)
        assert result == "ok"
        assert len(calls) == 2

    async def test_fatal_error_stops_immediately(self, fast_retry):
        calls = []

        async def f():
            calls.append(1)
            raise FatalLLMError("bad request")

        with pytest.raises(FatalLLMError):
            await retry_async(f, config=fast_retry)
        assert len(calls) == 1, "fatal error should not retry"

    async def test_budget_exceeded_stops_immediately(self, fast_retry):
        calls = []

        async def f():
            calls.append(1)
            raise BudgetExceededError("over cap")

        with pytest.raises(BudgetExceededError):
            await retry_async(f, config=fast_retry)
        assert len(calls) == 1, "budget errors should not retry"

    async def test_exhausts_and_reraises_last_error(self, fast_retry):
        calls = []

        async def f():
            calls.append(1)
            raise RetriableLLMError(f"flake {len(calls)}")

        with pytest.raises(RetriableLLMError, match=f"flake {fast_retry.max_attempts}"):
            await retry_async(f, config=fast_retry)
        assert len(calls) == fast_retry.max_attempts

    async def test_cancel_propagates_immediately(self, fast_retry):
        async def f():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await retry_async(f, config=fast_retry)

    async def test_keyboard_interrupt_propagates(self, fast_retry):
        async def f():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            await retry_async(f, config=fast_retry)

    async def test_unexpected_exception_is_retried(self, fast_retry):
        # A bare ValueError isn't classified — retry should treat it as retriable
        calls = []

        async def f():
            calls.append(1)
            if len(calls) < 2:
                raise ValueError("oops")
            return "ok"

        result = await retry_async(f, config=fast_retry)
        assert result == "ok"
        assert len(calls) == 2

    async def test_args_and_kwargs_threaded_through(self, fast_retry):
        async def f(a, b, *, c):
            return (a, b, c)

        result = await retry_async(f, 1, 2, c=3, config=fast_retry, op_name="x")
        assert result == (1, 2, 3)
