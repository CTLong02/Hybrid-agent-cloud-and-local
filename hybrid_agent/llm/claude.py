"""Claude Code SDK wrapper.

Uses `claude-agent-sdk` which spawns the local `claude` CLI as a subprocess.
Auth is handled by the CLI's existing OAuth login — no API key required.

The SDK lets us pass `cwd` (so Claude operates inside a sandbox), restrict
allowed tools, and read all messages/results in an async stream.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import ClaudeConfig
from ..cost import record_cost
from ..retry import FatalLLMError, RetriableLLMError
from .base import LLMClient, LLMResult

log = logging.getLogger(__name__)


class ClaudeClient(LLMClient):
    """Each instance can do one query at a time; concurrency is enforced by the
    LLMPool semaphore. We hold a `cwd` per call so different tasks can share
    the same client but operate in different sandboxes."""

    def __init__(self, config: ClaudeConfig, name: str = "claude") -> None:
        self.config = config
        self.name = name

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop: list[str] | None = None,
        cwd: Path | None = None,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
    ) -> LLMResult:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as exc:
            raise FatalLLMError(
                "claude-agent-sdk is not installed. `pip install claude-agent-sdk` "
                "and ensure Claude Code CLI is logged in (`claude` then sign in)."
            ) from exc

        options = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            cwd=str(cwd) if cwd else (self.config.cwd or None),
            allowed_tools=allowed_tools or self.config.allowed_tools,
            max_turns=self.config.max_turns,
            model=model,
        )

        try:
            chunks: list[str] = []
            ptoks = 0
            ctoks = 0
            async for msg in query(prompt=user_prompt, options=options):
                # Different SDK versions expose different message shapes.
                # Capture text content defensively.
                text = _extract_text(msg)
                if text:
                    chunks.append(text)
                # Token usage may live on a usage block in any message
                p, c = _extract_usage(msg)
                ptoks += p
                ctoks += c
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Treat all SDK errors as retriable; the caller's retry policy will
            # decide. Auth/login errors will surface in the message.
            raise RetriableLLMError(f"claude sdk error: {exc}") from exc

        text = "\n".join(chunks).strip()
        if not text:
            raise RetriableLLMError("claude sdk returned empty response")

        # Fallback estimate when the SDK didn't surface token counts.
        # ~4 chars/token is the well-known Anthropic rule of thumb.
        if ptoks == 0 and ctoks == 0:
            ptoks = max(1, len(system_prompt or "") // 4 + len(user_prompt) // 4)
            ctoks = max(1, len(text) // 4)

        resolved_model = model or "claude-code"
        record_cost(resolved_model, ptoks, ctoks)

        return LLMResult(
            text=text,
            model=resolved_model,
            prompt_tokens=ptoks,
            completion_tokens=ctoks,
        )

    async def health(self) -> bool:
        if not self.config.enabled:
            return False
        try:
            # Cheap probe: ask for a one-word reply.
            res = await asyncio.wait_for(
                self.complete(
                    system_prompt="You are a health probe. Reply with the single word OK.",
                    user_prompt="ping",
                ),
                timeout=30,
            )
            return "ok" in res.text.lower()
        except Exception as exc:  # noqa: BLE001
            log.warning("Claude health check failed: %s", exc)
            return False


def _extract_usage(msg) -> tuple[int, int]:
    """Best-effort token-count extraction from claude-agent-sdk messages.

    Returns (prompt_tokens, completion_tokens). Either may be 0 if the SDK
    didn't surface them on this message — the caller falls back to a
    character-based estimate when both totals stay 0.
    """
    usage = getattr(msg, "usage", None)
    if usage is None and isinstance(msg, dict):
        usage = msg.get("usage")
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        p = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        c = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    else:
        p = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
        c = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0) or 0
    try:
        return int(p), int(c)
    except (TypeError, ValueError):
        return 0, 0


def _extract_text(msg) -> str:
    """Best-effort text extraction from claude-agent-sdk message objects."""
    # Typical: msg has `.content` list of blocks; each block has `.type` and `.text`.
    text_parts: list[str] = []
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            t = getattr(block, "text", None)
            if t is None and isinstance(block, dict):
                t = block.get("text")
            if t:
                text_parts.append(str(t))
    elif isinstance(content, str):
        text_parts.append(content)
    # Fallback: top-level `result` or `text`
    if not text_parts:
        for attr in ("result", "text"):
            v = getattr(msg, attr, None)
            if v is None and isinstance(msg, dict):
                v = msg.get(attr)
            if isinstance(v, str) and v:
                text_parts.append(v)
                break
    return "\n".join(text_parts).strip()
