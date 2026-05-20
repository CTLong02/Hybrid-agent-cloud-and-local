"""Provider presets for the worker (OpenAI-compatible) pool.

The worker pool talks the OpenAI `/v1/chat/completions` shape, so any provider
that exposes that protocol drops in without a new client. These presets let
the UI auto-fill the base URL, default model, and env-var hint for the api key.

To probe a configured endpoint, use `probe(url, api_key, provider_id)` —
returns (ok, status_text).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    base_url: str
    default_model: str
    api_key_env: str  # informational hint
    needs_api_key: bool
    notes: str = ""


PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434",
        default_model="qwen3-coder:30b",
        api_key_env="",
        needs_api_key=False,
        notes="Local inference. Pull the model first with `ollama pull <name>`.",
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com",
        default_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        needs_api_key=True,
        notes=(
            "Reasoning models (o3/o4/gpt-5) reject `temperature` — leave temp at "
            "the default and let the model decide."
        ),
    ),
    ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-coder",
        api_key_env="DEEPSEEK_API_KEY",
        needs_api_key=True,
        notes="Strong on code at low USD/Mtok. `deepseek-reasoner` for thinking mode.",
    ),
    ProviderPreset(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz",
        default_model="qwen2.5-coder-32b-instruct",
        api_key_env="TOGETHER_API_KEY",
        needs_api_key=True,
        notes="Many open-weight models; pick one your prompt fits.",
    ),
    ProviderPreset(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai",
        default_model="llama-3.3-70b-versatile",
        api_key_env="GROQ_API_KEY",
        needs_api_key=True,
        notes="Very fast token throughput; latency-bound workloads.",
    ),
    ProviderPreset(
        id="fireworks",
        label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference",
        default_model="accounts/fireworks/models/qwen3-coder-480b",
        api_key_env="FIREWORKS_API_KEY",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="mistral",
        label="Mistral La Plateforme",
        base_url="https://api.mistral.ai",
        default_model="codestral-latest",
        api_key_env="MISTRAL_API_KEY",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="moonshot",
        label="Moonshot Kimi",
        base_url="https://api.moonshot.cn",
        default_model="kimi-k2-instruct",
        api_key_env="MOONSHOT_API_KEY",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="xai",
        label="xAI Grok",
        base_url="https://api.x.ai",
        default_model="grok-3-mini",
        api_key_env="XAI_API_KEY",
        needs_api_key=True,
    ),
    ProviderPreset(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api",
        default_model="qwen/qwen3-coder",
        api_key_env="OPENROUTER_API_KEY",
        needs_api_key=True,
        notes="Routes to dozens of model providers under one key.",
    ),
    ProviderPreset(
        id="custom",
        label="Custom (OpenAI-compatible)",
        base_url="",
        default_model="",
        api_key_env="",
        needs_api_key=False,
        notes="Any endpoint that speaks /v1/chat/completions.",
    ),
]


def by_id(pid: str) -> ProviderPreset:
    for p in PRESETS:
        if p.id == pid:
            return p
    return PRESETS[-1]  # custom


def guess_from(url: str) -> str:
    """Return the provider id whose base_url best matches `url`.

    Falls back to "custom" when nothing matches — used to seed the UI selector
    when loading an existing config.yaml.
    """
    if not url:
        return "custom"
    for p in PRESETS:
        if p.id == "custom" or not p.base_url:
            continue
        if url.rstrip("/") == p.base_url.rstrip("/"):
            return p.id
    # tolerant prefix match (e.g. http vs https, trailing /v1)
    for p in PRESETS:
        if p.id == "custom" or not p.base_url:
            continue
        host = p.base_url.split("://", 1)[-1].rstrip("/")
        if host and host in url:
            return p.id
    return "custom"


def probe(url: str, api_key: str | None, provider_id: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Hit a sanity-check endpoint on the provider.

    For Ollama we probe `/api/tags` (no auth). For everything else we probe
    `/v1/models` with bearer auth if a key is provided. Returns (ok, message).
    """
    if not url:
        return False, "no url"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    path = "/api/tags" if provider_id == "ollama" else "/v1/models"
    try:
        resp = httpx.get(url.rstrip("/") + path, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return False, f"timeout after {timeout}s contacting {url}"
    except httpx.NetworkError as exc:
        return False, f"network error: {exc}"

    if resp.status_code == 200:
        # Optionally hint how many models are listed.
        try:
            data = resp.json()
            n = 0
            if isinstance(data, dict):
                if isinstance(data.get("data"), list):
                    n = len(data["data"])
                elif isinstance(data.get("models"), list):
                    n = len(data["models"])
            return True, f"OK ({n} model(s) listed)" if n else "OK"
        except Exception:  # noqa: BLE001
            return True, "OK"

    if resp.status_code in (401, 403):
        return False, f"auth failed (status {resp.status_code}). Check api_key."
    if resp.status_code == 404:
        return False, (
            f"404 at {path}. The endpoint isn't OpenAI-compatible at this base URL — "
            "remove a trailing /v1 if present, or switch to 'Custom' and verify the URL."
        )
    body_preview = resp.text[:200].replace("\n", " ")
    return False, f"HTTP {resp.status_code}: {body_preview}"
