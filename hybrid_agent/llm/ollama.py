"""Ollama client via the OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import logging

import httpx

from ..config import LocalEndpointConfig
from ..cost import record_cost
from ..retry import FatalLLMError, RetriableLLMError, classify_http_error
from .base import LLMClient, LLMResult

log = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    def __init__(self, config: LocalEndpointConfig) -> None:
        self.config = config
        self.name = config.name
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.url,
                timeout=httpx.Timeout(self.config.timeout_seconds),
                headers=headers,
            )
        return self._client

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stop: list[str] | None = None,
    ) -> LLMResult:
        client = await self._http()
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": (temperature if temperature is not None else self.config.temperature),
            "stream": False,
        }
        if stop:
            body["stop"] = stop

        try:
            resp = await client.post("/v1/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetriableLLMError(f"network: {exc}") from exc

        if resp.status_code != 200:
            raise classify_http_error(resp.status_code, resp.text)

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FatalLLMError(f"unexpected response shape: {data}") from exc

        usage = data.get("usage", {}) or {}
        ptoks = int(usage.get("prompt_tokens", 0) or 0)
        ctoks = int(usage.get("completion_tokens", 0) or 0)
        # Charge to the active CostMeter (no-op if none bound). Local models
        # default to $0 — see DEFAULT_PRICING_PER_1M in cost.py.
        record_cost(self.config.model, ptoks, ctoks)
        return LLMResult(
            text=text,
            model=self.config.model,
            prompt_tokens=ptoks,
            completion_tokens=ctoks,
            raw=data,
        )

    async def health(self) -> bool:
        """Probe the endpoint. Works for both Ollama and cloud OpenAI-compatible
        providers (OpenAI, DeepSeek, Together, Groq, Fireworks, Mistral, ...).

        Ollama exposes /api/tags but not /v1/models without the OpenAI bridge;
        cloud providers expose /v1/models but not /api/tags. Try Ollama's
        endpoint first (cheap, no auth needed locally) then fall back to the
        OpenAI-spec one.
        """
        try:
            client = await self._http()
            for path in ("/api/tags", "/v1/models"):
                try:
                    resp = await client.get(path, timeout=5.0)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    log.warning("Health probe %s%s network error: %s", self.config.url, path, exc)
                    return False
                if resp.status_code == 200:
                    return True
                # 401/403 means the endpoint exists but auth failed — surface
                # that as unhealthy so the router escalates, but log loudly.
                if resp.status_code in (401, 403):
                    log.warning(
                        "Health probe %s%s auth failed (status %d) — check api_key",
                        self.config.url,
                        path,
                        resp.status_code,
                    )
                    return False
                # 404 on this path: try the next one.
            log.warning(
                "Health probe failed for %s: both /api/tags and /v1/models returned non-200",
                self.config.url,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("Ollama-style health check failed for %s: %s", self.name, exc)
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
