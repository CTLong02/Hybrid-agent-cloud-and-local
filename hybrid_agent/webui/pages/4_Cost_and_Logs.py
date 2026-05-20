"""Cost + Logs page — token spend per task/model and a live log tail.

Cost is derived from the persisted log lines plus state DB attempts. Streamlit
isn't sharing memory with the running orchestrator, so we can't grab the
in-memory CostMeter directly — but the orchestrator also writes the cost
summary to the log file at end-of-run, and per-call logs include token
counts. For live token totals, we re-parse the run log file.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import pandas as pd
import streamlit as st

from hybrid_agent.webui import process as proc
from hybrid_agent.webui.shared import load_config, project_root, setup_page

setup_page("Cost & Logs", icon="[$]")
st.title("Cost & Logs")

cfg = load_config()

tab_cost, tab_log = st.tabs(["Cost summary", "Live log"])

# ---- Cost ------------------------------------------------------------------

with tab_cost:
    st.caption(
        "Parsed from the orchestrator's log file. End-of-run summaries are the "
        "most accurate; per-call usage shows up live."
    )

    log_path = None
    if cfg and cfg.log_file:
        log_path = Path(cfg.log_file)
        if not log_path.is_absolute():
            log_path = project_root() / log_path

    if log_path is None or not log_path.exists():
        st.info(
            "No `log_file` configured (or file doesn't exist yet). Set "
            "`log_file: hybrid_agent.log` in config.yaml to enable cost tracking here."
        )
    else:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        # Find the most recent "Cost summary:" block at end of file.
        summaries = list(re.finditer(r"Cost summary:.*?(?=\n\S|\Z)", text, re.DOTALL))
        if summaries:
            st.subheader("Latest end-of-run cost summary")
            st.code(summaries[-1].group(0), language="text")
        else:
            st.caption("No end-of-run cost summary in log yet.")

        # Per-call breakdown — match lines like:
        # "...claude-sonnet-4-5 prompt=1234 completion=567 usd=0.0123"
        # Try several patterns the cost meter / clients emit.
        call_pattern = re.compile(
            r"(?P<model>[\w\-\.\:/]+)\s+prompt=(?P<p>\d+)\s+completion=(?P<c>\d+)"
            r"(?:\s+usd=(?P<usd>[\d\.]+))?"
        )
        records = []
        for m in call_pattern.finditer(text):
            records.append(
                {
                    "model": m.group("model"),
                    "prompt_tokens": int(m.group("p")),
                    "completion_tokens": int(m.group("c")),
                    "usd": float(m.group("usd")) if m.group("usd") else 0.0,
                }
            )
        if records:
            df = pd.DataFrame(records)
            agg = (
                df.groupby("model")
                .agg(
                    calls=("model", "count"),
                    prompt_tokens=("prompt_tokens", "sum"),
                    completion_tokens=("completion_tokens", "sum"),
                    usd=("usd", "sum"),
                )
                .reset_index()
                .sort_values("usd", ascending=False)
            )
            st.subheader("Per-model usage (parsed from log)")
            st.dataframe(agg, use_container_width=True, hide_index=True)
            total_usd = float(agg["usd"].sum())
            total_calls = int(agg["calls"].sum())
            st.metric("Total spend (parsed)", f"${total_usd:.4f}", delta=f"{total_calls} calls")
        else:
            st.caption("No per-call usage lines matched yet.")

# ---- Live log --------------------------------------------------------------

with tab_log:
    auto = st.toggle(
        "Auto-refresh every 2s",
        value=proc.read_handle(project_root()) is not None,
    )
    n_bytes = st.slider("Tail size (KB)", 10, 1000, 200, step=10)

    text = proc.tail_log(project_root(), max_bytes=n_bytes * 1024)

    # also show the configured log file if present (it has all history,
    # not just the current webui-launched run)
    if cfg and cfg.log_file:
        cfg_log = Path(cfg.log_file)
        if not cfg_log.is_absolute():
            cfg_log = project_root() / cfg_log
        if cfg_log.exists():
            size = cfg_log.stat().st_size
            with open(cfg_log, "rb") as f:
                if size > n_bytes * 1024:
                    f.seek(size - n_bytes * 1024)
                    f.readline()
                hist = f.read().decode("utf-8", errors="replace")
            with st.expander(f"Project log file ({cfg_log.name}, last {n_bytes} KB)", expanded=False):
                st.code(hist, language="log")

    st.subheader("Webui-launched run output")
    if text:
        st.code(text, language="log")
    else:
        st.caption("No webui-launched run output yet.")

    if auto:
        time.sleep(2)
        st.rerun()
