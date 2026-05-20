"""Config page — form editor for config.yaml.

Covers every section the orchestrator reads: project root, logging, state DB,
sandbox, routing, retry, orchestrator timeouts, cost caps, local endpoints,
and Claude SDK settings. Also exposes the raw YAML editor as an escape hatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import streamlit as st
import yaml

from hybrid_agent.config import AppConfig
from hybrid_agent.webui import process as proc
from hybrid_agent.webui.provider_presets import PRESETS, by_id, guess_from, probe
from hybrid_agent.webui.shared import config_path, load_config_raw, project_root, setup_page

setup_page("Config", icon="[C]")
st.title("Configuration")
st.caption(f"Editing `{config_path()}`")

if proc.read_handle(project_root()) is not None:
    st.warning(
        "A run is in progress. Config changes will only take effect on the next run."
    )

raw = load_config_raw()


def _g(section: str, key: str, default=None):
    return (raw.get(section) or {}).get(key, default)


def _list_str(val) -> str:
    if not val:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val)


def _parse_list(s: str) -> list[str]:
    if not s.strip():
        return []
    return [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]


tab_general, tab_models, tab_routing, tab_cost, tab_orch, tab_raw = st.tabs(
    ["General", "Models", "Routing", "Cost", "Orchestrator", "Raw YAML"]
)

# ---- General --------------------------------------------------------------

with tab_general:
    c1, c2 = st.columns(2)
    project_root_val = c1.text_input(
        "project_root",
        value=raw.get("project_root", "."),
        help="Working directory the agent operates on.",
    )
    log_level_val = c2.selectbox(
        "log_level",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
        index=["DEBUG", "INFO", "WARNING", "ERROR"].index(
            str(raw.get("log_level", "INFO")).upper()
        ),
    )
    c1, c2 = st.columns(2)
    log_file_val = c1.text_input("log_file", value=raw.get("log_file") or "")
    log_json_val = c2.text_input("log_json_file", value=raw.get("log_json_file") or "")

    st.subheader("Sandbox")
    sb_c1, sb_c2 = st.columns(2)
    sandbox_dir = sb_c1.text_input(
        "sandbox.base_dir", value=_g("sandbox", "base_dir", ".hybrid_agent_sandboxes")
    )
    sandbox_branch_prefix = sb_c2.text_input(
        "sandbox.branch_prefix", value=_g("sandbox", "branch_prefix", "agent/")
    )
    sb_c1, sb_c2 = st.columns(2)
    sandbox_use_worktree = sb_c1.checkbox(
        "sandbox.use_git_worktree", value=bool(_g("sandbox", "use_git_worktree", True))
    )
    sandbox_base_branch = sb_c2.text_input(
        "sandbox.base_branch", value=_g("sandbox", "base_branch", "main")
    )

    st.subheader("State")
    state_db = st.text_input("state.db_path", value=_g("state", "db_path", ".hybrid_agent_state.db"))

# ---- Models ---------------------------------------------------------------

with tab_models:
    st.subheader("Worker pool — local Ollama OR cloud (OpenAI-compatible)")
    st.caption(
        "Any provider that speaks OpenAI's `/v1/chat/completions` works here "
        "(OpenAI, DeepSeek, Together, Groq, Fireworks, Mistral, Moonshot, xAI, "
        "OpenRouter, …). Endpoints in this pool are scheduled by routing as "
        "the `local` backend — each one serves one task at a time, so add more "
        "for parallelism."
    )

    eps = (raw.get("local") or {}).get("endpoints") or [
        {
            "name": "ollama-default",
            "url": "http://localhost:11434",
            "model": "qwen3-coder:30b",
            "timeout_seconds": 600,
            "max_tokens": 8192,
            "temperature": 0.2,
        }
    ]
    n = st.number_input(
        "Number of endpoints",
        min_value=1,
        max_value=8,
        value=len(eps),
        step=1,
    )
    while len(eps) < n:
        eps.append(dict(eps[-1]))
    eps = eps[:n]

    preset_labels = [p.label for p in PRESETS]

    new_endpoints = []
    for i, ep in enumerate(eps):
        current_provider = guess_from(ep.get("url", ""))
        with st.expander(
            f"Endpoint #{i + 1}: {ep.get('name', '')}  ·  {by_id(current_provider).label}",
            expanded=(i == 0),
        ):
            # ---- Provider preset ----------------------------------------
            provider_idx = next(
                (j for j, p in enumerate(PRESETS) if p.id == current_provider), 0
            )
            picked_label = st.selectbox(
                "Provider preset",
                options=preset_labels,
                index=provider_idx,
                key=f"provider##{i}",
                help=(
                    "Pick a preset to auto-fill base URL and a sensible default model. "
                    "Choose 'Custom' for anything not listed."
                ),
            )
            preset = PRESETS[preset_labels.index(picked_label)]

            apply_preset = st.button(
                f"Apply '{preset.label}' defaults",
                key=f"apply_preset##{i}",
                disabled=(preset.id == current_provider and preset.id != "custom"),
                help=(
                    "Overwrites the URL with the provider's base URL and the model "
                    "field with the provider's default model. Other fields untouched."
                ),
            )
            if apply_preset:
                # Force the text_input widgets (rendered below) to pick up the
                # preset values on the immediate rerun. Setting the raw ep
                # dict alone isn't enough: Streamlit widgets remember their
                # last user value via session_state once they've rendered.
                st.session_state[f"endpoint_url_{i}"] = preset.base_url
                if preset.default_model:
                    st.session_state[f"endpoint_model_{i}"] = preset.default_model
                st.rerun()

            if preset.notes:
                st.caption(preset.notes)
            if preset.needs_api_key and preset.api_key_env:
                st.caption(
                    f"This provider needs an API key. Common env var: "
                    f"`{preset.api_key_env}`."
                )

            # ---- Core endpoint fields -----------------------------------
            r1, r2 = st.columns(2)
            name = r1.text_input(
                f"name##{i}", value=ep.get("name", f"{preset.id}-{i}"),
                help="Friendly name used in logs and the cost summary.",
            )
            url = r2.text_input(
                f"base URL##{i}",
                value=ep.get("url", preset.base_url or "http://localhost:11434"),
                key=f"endpoint_url_{i}",
                help=(
                    "Base URL — the client appends `/v1/chat/completions`. "
                    "Do NOT include a trailing `/v1`."
                ),
            )

            r1, r2 = st.columns(2)
            model = r1.text_input(
                f"model##{i}",
                value=ep.get("model", preset.default_model or "qwen3-coder:30b"),
                key=f"endpoint_model_{i}",
                help="Provider-specific model identifier.",
            )
            api_key = r2.text_input(
                f"api_key##{i}",
                value=ep.get("api_key") or "",
                type="password",
                help=(
                    "Sent as `Authorization: Bearer …`. Leave empty for Ollama "
                    f"or any provider that doesn't need auth."
                    + (f" Env hint: ${preset.api_key_env}" if preset.api_key_env else "")
                ),
            )

            r1, r2, r3 = st.columns(3)
            timeout = r1.number_input(
                f"timeout_seconds##{i}",
                min_value=10,
                max_value=7200,
                value=int(ep.get("timeout_seconds", 600)),
            )
            max_tokens = r2.number_input(
                f"max_tokens##{i}",
                min_value=128,
                max_value=131072,
                value=int(ep.get("max_tokens", 8192)),
                help="Max completion tokens per request.",
            )
            temperature = r3.number_input(
                f"temperature##{i}",
                min_value=0.0,
                max_value=2.0,
                step=0.05,
                value=float(ep.get("temperature", 0.2)),
                help=(
                    "OpenAI o-series / gpt-5 ignore this — they pick their own. "
                    "Set to 0 for deterministic-ish coding output."
                ),
            )

            # ---- Test connection ----------------------------------------
            test_col, _ = st.columns([1, 3])
            if test_col.button("Test connection", key=f"test##{i}"):
                with st.spinner(f"Probing {url}…"):
                    ok, msg = probe(url, api_key or None, preset.id)
                if ok:
                    st.success(f"{name}: {msg}")
                else:
                    st.error(f"{name}: {msg}")

            new_endpoints.append(
                {
                    "name": name,
                    "url": url,
                    "model": model,
                    "timeout_seconds": int(timeout),
                    "max_tokens": int(max_tokens),
                    "temperature": float(temperature),
                    **({"api_key": api_key} if api_key else {}),
                }
            )

    st.divider()
    st.subheader("Claude (planner + reviewer)")
    claude_enabled = st.checkbox(
        "claude.enabled", value=bool(_g("claude", "enabled", True))
    )
    cc1, cc2 = st.columns(2)
    claude_concurrency = cc1.number_input(
        "claude.concurrency",
        min_value=1,
        max_value=16,
        value=int(_g("claude", "concurrency", 1)),
    )
    claude_max_turns = cc2.number_input(
        "claude.max_turns",
        min_value=1,
        max_value=200,
        value=int(_g("claude", "max_turns", 30)),
    )
    cc1, cc2, cc3 = st.columns(3)
    claude_planner_model = cc1.text_input(
        "claude.planner_model",
        value=_g("claude", "planner_model") or "",
        help="Optional override; empty = SDK default.",
    )
    claude_coder_model = cc2.text_input(
        "claude.coder_model", value=_g("claude", "coder_model") or ""
    )
    claude_reviewer_model = cc3.text_input(
        "claude.reviewer_model", value=_g("claude", "reviewer_model") or ""
    )
    claude_task_generator_model = st.text_input(
        "claude.task_generator_model",
        value=_g("claude", "task_generator_model") or "",
        help="Model used by `hybrid-agent generate-tasks`. Empty = SDK default.",
    )
    claude_cwd = st.text_input(
        "claude.cwd",
        value=_g("claude", "cwd") or "",
        help="Defaults to project_root if empty.",
    )
    claude_tools = st.text_input(
        "claude.allowed_tools",
        value=_list_str(
            _g("claude", "allowed_tools", ["Read", "Write", "Edit", "Bash", "Grep", "Glob"])
        ),
    )

# ---- Routing --------------------------------------------------------------

with tab_routing:
    routing = raw.get("routing") or {}
    rc1, rc2 = st.columns(2)
    default_backend = rc1.selectbox(
        "default_backend",
        options=["local", "claude"],
        index=["local", "claude"].index(str(routing.get("default_backend", "local"))),
    )
    escalate_after = rc2.number_input(
        "escalate_to_claude_after_failures",
        min_value=0,
        max_value=20,
        value=int(routing.get("escalate_to_claude_after_failures", 2)),
    )
    force_claude_tags = st.text_input(
        "force_claude_tags",
        value=_list_str(
            routing.get("force_claude_tags", ["security", "auth", "payment", "claude_only"])
        ),
    )
    force_local_tags = st.text_input(
        "force_local_tags", value=_list_str(routing.get("force_local_tags", ["local_only"]))
    )
    claude_for_complexity = st.text_input(
        "claude_for_complexity",
        value=_list_str(routing.get("claude_for_complexity", ["high"])),
        help="Complexity levels (low / medium / high) that force Claude.",
    )
    claude_path_globs = st.text_input(
        "claude_path_globs",
        value=_list_str(routing.get("claude_path_globs", [])),
        help="Files matching these globs go to Claude (e.g. `migrations/*.sql`).",
    )

# ---- Cost ----------------------------------------------------------------

with tab_cost:
    cost = raw.get("cost") or {}
    c1, c2, c3 = st.columns(3)
    per_task_cap = c1.number_input(
        "per_task_usd_cap",
        min_value=0.0,
        value=float(cost.get("per_task_usd_cap") or 0.0),
        step=0.5,
        help="0 = no cap.",
    )
    per_run_cap = c2.number_input(
        "per_run_usd_cap",
        min_value=0.0,
        value=float(cost.get("per_run_usd_cap") or 0.0),
        step=1.0,
        help="0 = no cap.",
    )
    on_exceed = c3.selectbox(
        "on_exceed",
        options=["stop", "warn"],
        index=["stop", "warn"].index(str(cost.get("on_exceed", "stop"))),
    )
    pricing_overrides_text = st.text_area(
        "pricing_overrides (YAML)",
        value=yaml.safe_dump(cost.get("pricing_overrides", {}) or {}, sort_keys=False),
        height=140,
        help="model -> {input, output} in USD per 1M tokens.",
    )

# ---- Orchestrator --------------------------------------------------------

with tab_orch:
    orch = raw.get("orchestrator") or {}
    o1, o2 = st.columns(2)
    max_conc = o1.number_input(
        "max_concurrent_tasks",
        min_value=1,
        max_value=64,
        value=int(orch.get("max_concurrent_tasks", 4)),
    )
    fail_fast = o2.checkbox("fail_fast", value=bool(orch.get("fail_fast", False)))
    o1, o2 = st.columns(2)
    auto_fix = o1.checkbox(
        "auto_fix_on_review", value=bool(orch.get("auto_fix_on_review", True))
    )
    review_max_iter = o2.number_input(
        "review_max_iterations",
        min_value=0,
        max_value=10,
        value=int(orch.get("review_max_iterations", 2)),
    )

    st.subheader("Stage timeouts (seconds)")
    t1, t2, t3 = st.columns(3)
    plan_t = t1.number_input(
        "plan_timeout_seconds",
        min_value=10,
        max_value=86400,
        value=int(orch.get("plan_timeout_seconds", 600)),
    )
    code_t = t2.number_input(
        "code_timeout_seconds",
        min_value=10,
        max_value=86400,
        value=int(orch.get("code_timeout_seconds", 1800)),
    )
    review_t = t3.number_input(
        "review_timeout_seconds",
        min_value=10,
        max_value=86400,
        value=int(orch.get("review_timeout_seconds", 600)),
    )

    st.subheader("Test gate")
    g1, g2, g3 = st.columns(3)
    run_tests = g1.checkbox(
        "run_tests_in_review", value=bool(orch.get("run_tests_in_review", True))
    )
    require_tests = g2.checkbox(
        "require_tests", value=bool(orch.get("require_tests", False))
    )
    test_timeout = g3.number_input(
        "test_timeout_seconds",
        min_value=10,
        max_value=86400,
        value=int(orch.get("test_timeout_seconds", 300)),
    )
    test_command = st.text_input(
        "test_command", value=orch.get("test_command") or "", help="Empty = auto-detect."
    )

    st.subheader("System test (merge --review)")
    s1, s2 = st.columns(2)
    system_test_cmd = s1.text_input(
        "system_test_command",
        value=orch.get("system_test_command") or "",
        help="Empty = auto-detect.",
    )
    system_test_to = s2.number_input(
        "system_test_timeout_seconds",
        min_value=10,
        max_value=86400,
        value=int(orch.get("system_test_timeout_seconds", 900)),
    )

# ---- Save / Raw YAML -----------------------------------------------------


def _build_dict() -> dict:
    """Assemble the form values back into a dict ready to dump as YAML."""
    try:
        pricing_overrides = yaml.safe_load(pricing_overrides_text) or {}
        if not isinstance(pricing_overrides, dict):
            raise ValueError("pricing_overrides must be a YAML mapping")
    except Exception as exc:
        raise ValueError(f"pricing_overrides YAML invalid: {exc}") from exc

    out: dict = {
        "project_root": project_root_val,
        "log_level": log_level_val,
        "log_file": log_file_val or None,
        "log_json_file": log_json_val or None,
        "state": {"db_path": state_db},
        "sandbox": {
            "base_dir": sandbox_dir,
            "use_git_worktree": sandbox_use_worktree,
            "base_branch": sandbox_base_branch,
            "branch_prefix": sandbox_branch_prefix,
        },
        "routing": {
            "default_backend": default_backend,
            "escalate_to_claude_after_failures": int(escalate_after),
            "force_claude_tags": _parse_list(force_claude_tags),
            "force_local_tags": _parse_list(force_local_tags),
            "claude_for_complexity": _parse_list(claude_for_complexity),
            "claude_path_globs": _parse_list(claude_path_globs),
        },
        "cost": {
            "per_task_usd_cap": float(per_task_cap) if per_task_cap > 0 else None,
            "per_run_usd_cap": float(per_run_cap) if per_run_cap > 0 else None,
            "on_exceed": on_exceed,
            "pricing_overrides": pricing_overrides,
        },
        "orchestrator": {
            "max_concurrent_tasks": int(max_conc),
            "fail_fast": fail_fast,
            "auto_fix_on_review": auto_fix,
            "review_max_iterations": int(review_max_iter),
            "plan_timeout_seconds": int(plan_t),
            "code_timeout_seconds": int(code_t),
            "review_timeout_seconds": int(review_t),
            "run_tests_in_review": run_tests,
            "require_tests": require_tests,
            "test_timeout_seconds": int(test_timeout),
            "test_command": test_command or None,
            "system_test_command": system_test_cmd or None,
            "system_test_timeout_seconds": int(system_test_to),
        },
        "local": {"endpoints": new_endpoints},
        "claude": {
            "enabled": claude_enabled,
            "concurrency": int(claude_concurrency),
            "max_turns": int(claude_max_turns),
            "allowed_tools": _parse_list(claude_tools),
            **({"cwd": claude_cwd} if claude_cwd else {}),
            **(
                {"planner_model": claude_planner_model}
                if claude_planner_model
                else {}
            ),
            **(
                {"coder_model": claude_coder_model} if claude_coder_model else {}
            ),
            **(
                {"reviewer_model": claude_reviewer_model}
                if claude_reviewer_model
                else {}
            ),
            **(
                {"task_generator_model": claude_task_generator_model}
                if claude_task_generator_model
                else {}
            ),
        },
    }
    return out


with tab_raw:
    yaml_current = yaml.safe_dump(raw, sort_keys=False)
    edited = st.text_area("config.yaml (raw)", value=yaml_current, height=600)
    if st.button("Save raw YAML", type="primary", key="raw_yaml_save"):
        try:
            parsed = yaml.safe_load(edited) or {}
            AppConfig(**parsed)  # validate
            config_path().write_text(edited, encoding="utf-8")
            st.success("Saved. Takes effect on next run.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Invalid config: {exc}")

st.divider()

save_col, _ = st.columns([1, 5])
if save_col.button("Save form values", type="primary"):
    try:
        new_dict = _build_dict()
        AppConfig(**new_dict)  # validate
        config_path().write_text(
            yaml.safe_dump(new_dict, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        st.success(f"Saved {config_path().name}. Takes effect on next run.")
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Save failed: {exc}")
