"""Retry helpers. Distinguishes retriable vs fatal errors so we don't waste
attempts on bad prompts / context-overflow / auth failures."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .config import RetryConfig

log = logging.getLogger(__name__)

T = TypeVar("T")


class FatalLLMError(Exception):
    """Non-retriable. E.g. auth failure, malformed request, context overflow."""


class RetriableLLMError(Exception):
    """Network blip, 5xx, timeout, transient rate-limit."""


def classify_http_error(status_code: int, body: str = "") -> Exception:
    """Map an HTTP status to either Fatal or Retriable."""
    if status_code in (401, 403):
        return FatalLLMError(f"auth error {status_code}: {body[:200]}")
    if status_code == 400:
        # Often context too long or malformed prompt — not worth retrying
        return FatalLLMError(f"bad request {status_code}: {body[:200]}")
    if status_code == 404:
        return FatalLLMError(f"not found {status_code}: {body[:200]}")
    if status_code == 429:
        return RetriableLLMError(f"rate limited {status_code}: {body[:200]}")
    if 500 <= status_code < 600:
        return RetriableLLMError(f"server error {status_code}: {body[:200]}")
    return RetriableLLMError(f"http error {status_code}: {body[:200]}")


async def retry_async(
    func: Callable[..., Awaitable[T]],
    *args,
    config: RetryConfig,
    op_name: str = "call",
    **kwargs,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Stops immediately on FatalLLMError. Retries any other exception up to
    `config.max_attempts`. Last exception is re-raised on exhaustion.
    """
    # Late import to avoid a circular dependency (cost.py imports retry's
    # exception types in turn for tests).
    from .cost import BudgetExceededError

    last_exc: Exception | None = None
    delay = config.initial_delay_seconds

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except FatalLLMError:
            log.error("%s: fatal error, not retrying", op_name)
            raise
        except BudgetExceededError:
            log.error("%s: budget exceeded, not retrying", op_name)
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= config.max_attempts:
                log.error("%s: exhausted %d attempts: %s", op_name, attempt, exc)
                break

            sleep_for = min(delay, config.max_delay_seconds)
            if config.jitter:
                sleep_for *= 0.5 + random.random()
            log.warning(
                "%s: attempt %d/%d failed (%s); sleeping %.1fs",
                op_name,
                attempt,
                config.max_attempts,
                exc,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay *= config.exponential_base

    assert last_exc is not None
    raise last_exc
