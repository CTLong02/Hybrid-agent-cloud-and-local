"""LLM client abstraction.

Two implementations exist:
  - OllamaClient: HTTP to a local OpenAI-compatible endpoint
  - ClaudeClient: subprocess-style via claude-agent-sdk (no API key)

Both expose a uniform `complete()` returning text, plus pool-friendly
`acquire()` / `release()` semaphores so the orchestrator can cap concurrency.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict | None = None


class LLMClient(ABC):
    name: str

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop: list[str] | None = None,
    ) -> LLMResult: ...

    @abstractmethod
    async def health(self) -> bool: ...


class LLMPool:
    """A pool of one or more clients with a semaphore per pool.

    Local pool: concurrency = number of endpoints (each endpoint = 1 slot).
    Claude pool: concurrency = config.claude.concurrency (typically 1).
    """

    def __init__(self, clients: list[LLMClient], concurrency: int | None = None) -> None:
        if not clients:
            raise ValueError("LLMPool needs at least one client")
        self.clients = clients
        self.concurrency = concurrency or len(clients)
        self._sem = asyncio.Semaphore(self.concurrency)
        self._round_robin = 0
        self._lock = asyncio.Lock()
        self._healthy: dict[str, bool] = {c.name: True for c in clients}

    async def health(self) -> bool:
        results = await asyncio.gather(*(c.health() for c in self.clients), return_exceptions=True)
        any_healthy = False
        for c, r in zip(self.clients, results, strict=True):
            ok = r is True
            self._healthy[c.name] = ok
            any_healthy = any_healthy or ok
        return any_healthy

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[LLMClient]:
        await self._sem.acquire()
        try:
            async with self._lock:
                # Round-robin among healthy clients; fallback to any
                healthy = [c for c in self.clients if self._healthy.get(c.name, True)]
                pool = healthy or self.clients
                client = pool[self._round_robin % len(pool)]
                self._round_robin += 1
            yield client
        finally:
            self._sem.release()
