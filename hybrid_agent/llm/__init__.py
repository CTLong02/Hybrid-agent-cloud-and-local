"""LLM package: pool builders."""

from __future__ import annotations

from ..config import AppConfig
from .base import LLMClient, LLMPool, LLMResult
from .claude import ClaudeClient
from .ollama import OllamaClient


def build_local_pool(config: AppConfig) -> LLMPool:
    clients: list[LLMClient] = [OllamaClient(ep) for ep in config.local.endpoints]
    return LLMPool(clients, concurrency=len(clients))


def build_claude_pool(config: AppConfig) -> LLMPool | None:
    if not config.claude.enabled:
        return None
    client = ClaudeClient(config.claude)
    return LLMPool([client], concurrency=config.claude.concurrency)


__all__ = [
    "LLMClient",
    "LLMPool",
    "LLMResult",
    "build_local_pool",
    "build_claude_pool",
    "OllamaClient",
    "ClaudeClient",
]
