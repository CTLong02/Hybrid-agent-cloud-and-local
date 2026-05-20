"""Merge page — drive `hybrid-agent merge` from the UI.

Exposes every flag of the CLI, including the post-merge `--cleanup` that
tears down per-task worktrees and `agent/<task_id>` branches.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import streamlit as st

from hybrid_agent.webui.shared import (
    config_path,
    load_config,
    setup_page,
    tasks_path,
)

setup_page("Merge", icon="[M]")

st.title("Merge task sandboxes")
st.caption(
    "Combine every DONE-task sandbox into one output tree. Later tasks "
    "in dependency order overwrite earlier ones on file conflicts."
)

cfg = load_config()
if cfg is None:
    st.error("Config missing — fix the Config page first.")
    st.stop()

# ---- Form ----------------------------------------------------------------

col_a, col_b = st.columns(2)
with col_a:
    output = st.text_input("Output directory", value="merged")
    dry_run = st.checkbox(
        "Dry run",
        value=False,
        help="Print what would be copied without writing. Disables --cleanup.",
    )
    cleanup = st.checkbox(
        "Cleanup after merge",
        value=False,
        help=(
            "Remove per-task sandboxes and `agent/<task_id>` branches after a "
            "successful merge (and review gate, if enabled). Only touches "
            "tasks whose files contributed. No-op with Dry run."
        ),
    )

with col_b:
    review = st.checkbox(
        "Run review gate",
        value=False,
        help=(
            "Post-merge: deterministic reconcile + py_compile + project tests "
            "+ optional system test. Exits non-zero on hard failure."
        ),
    )
    llm_review = st.checkbox(
        "LLM (Claude) semantic review",
        value=False,
        help="Adds one Claude call inspecting the merged tree. Implies review.",
        disabled=not cfg.claude.enabled,
    )
    llm_review_mode = st.selectbox(
        "LLM review mode",
        options=["inline", "tools"],
        index=0,
        disabled=not llm_review,
        help=(
            "'inline' embeds conflict files in the prompt (fast, deterministic). "
            "'tools' lets Claude Read/Grep/Glob — more thorough but flakier on Windows."
        ),
    )

llm_fix = st.checkbox(
    "Auto-fix on FAIL (--llm-fix)",
    value=False,
    help="When LLM review verdict is FAIL, Claude edits the merged tree to fix it. Implies LLM review.",
    disabled=not cfg.claude.enabled,
)
fix_col_a, fix_col_b = st.columns(2)
with fix_col_a:
    llm_fix_iterations = st.number_input(
        "Fix iterations",
        min_value=1,
        max_value=10,
        value=3,
        disabled=not llm_fix,
    )
with fix_col_b:
    llm_fix_mode = st.selectbox(
        "Fix mode",
        options=["patch", "tools"],
        index=0,
        disabled=not llm_fix,
        help=(
            "'patch' = JSON-patch applied deterministically (avoids Windows "
            "Claude CLI flake). 'tools' = Claude Edit/Write directly via SDK."
        ),
    )

# Show a preview of the command that will be executed.
cmd_preview = [
    "hybrid-agent",
    "merge",
    "-c",
    config_path().name,
    "-t",
    tasks_path().name,
    "-o",
    output or "merged",
]
if dry_run:
    cmd_preview.append("--dry-run")
if review:
    cmd_preview.append("--review")
if llm_review:
    cmd_preview += ["--llm-review", "--llm-review-mode", llm_review_mode]
if llm_fix:
    cmd_preview += [
        "--llm-fix",
        "--llm-fix-iterations",
        str(int(llm_fix_iterations)),
        "--llm-fix-mode",
        llm_fix_mode,
    ]
if cleanup and not dry_run:
    cmd_preview.append("--cleanup")
st.code(" ".join(cmd_preview), language="bash")

# ---- Execute -------------------------------------------------------------

run_btn = st.button("Run merge", type="primary")

if run_btn:
    cmd = [
        sys.executable,
        "-m",
        "hybrid_agent",
        "merge",
        "-c",
        str(config_path()),
        "-t",
        str(tasks_path()),
        "-o",
        output or "merged",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if review:
        cmd.append("--review")
    if llm_review:
        cmd += ["--llm-review", "--llm-review-mode", llm_review_mode]
    if llm_fix:
        cmd += [
            "--llm-fix",
            "--llm-fix-iterations",
            str(int(llm_fix_iterations)),
            "--llm-fix-mode",
            llm_fix_mode,
        ]
    if cleanup and not dry_run:
        cmd.append("--cleanup")

    # Merge typically completes in seconds; review can take minutes (Claude).
    timeout_s = 900 if (llm_review or llm_fix) else 120

    with st.spinner(f"Running merge (timeout {timeout_s}s)…"):
        started = time.time()
        # `project_root: ./my-codebase` in config.yaml is resolved relative to
        # CWD, so we invoke from the directory holding config.yaml — that's
        # how the user runs the CLI by hand.
        cwd = str(config_path().parent)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            st.error(f"Merge timed out after {timeout_s}s.")
            result = None
        except Exception as exc:  # noqa: BLE001
            st.error(f"Merge failed to start: {exc}")
            result = None

    if result is not None:
        elapsed = time.time() - started
        if result.returncode == 0:
            # Pull the most useful one-line summary out of stdout.
            lines = (result.stdout or "").splitlines()
            summary = next(
                (
                    line.strip()
                    for line in reversed(lines)
                    if line.strip().startswith(("Merged ", "Dry run", "Cleaned "))
                ),
                f"Merge completed in {elapsed:.1f}s.",
            )
            st.success(summary)
            if cleanup and not dry_run:
                cleaned_lines = [
                    line for line in lines if "cleaned" in line.lower() and "T" in line
                ]
                if cleaned_lines:
                    with st.expander(f"Cleanup detail ({len(cleaned_lines)} task(s))", expanded=False):
                        st.code("\n".join(cleaned_lines), language="text")
            with st.expander("Full stdout", expanded=False):
                st.code(result.stdout or "(empty)", language="text")
        else:
            st.error(f"merge failed (exit {result.returncode}, {elapsed:.1f}s).")
            if result.stdout:
                with st.expander("stdout", expanded=False):
                    st.code(result.stdout, language="text")
            if result.stderr:
                with st.expander("stderr", expanded=True):
                    st.code(result.stderr, language="text")
