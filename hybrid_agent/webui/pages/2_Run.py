"""Run page — start/stop the pipeline and follow live status."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import pandas as pd
import streamlit as st

from hybrid_agent.models import TaskStatus
from hybrid_agent.parsers import parse_file
from hybrid_agent.state import StateStore
from hybrid_agent.webui import process as proc
from hybrid_agent.webui.shared import (
    config_path,
    load_config,
    project_root,
    setup_page,
    state_db_path,
    tasks_path,
)

setup_page("Run", icon="[R]")
st.title("Run pipeline")

cfg = load_config()
if cfg is None:
    st.error("Could not load config.yaml — fix it on the Config page first.")
    st.stop()

workdir = project_root()
handle = proc.read_handle(workdir)

# ---- control bar ----------------------------------------------------------

ctrl_l, ctrl_r = st.columns([3, 2])

with ctrl_l:
    if handle is None:
        st.success("Pipeline idle.")
    else:
        elapsed = int(time.time() - handle.started_at)
        st.warning(
            f"RUNNING — PID {handle.pid} · started "
            f"{datetime.fromtimestamp(handle.started_at).strftime('%H:%M:%S')} · "
            f"elapsed {elapsed}s"
        )

with ctrl_r:
    auto_resume = st.number_input(
        "auto-resume rounds",
        min_value=0,
        max_value=20,
        value=0,
        help=(
            "After the run finishes, if any tasks are FAILED/BLOCKED, reset them "
            "and re-run, up to N extra rounds."
        ),
    )

btn_start, btn_stop, btn_kill, btn_refresh = st.columns(4)

if btn_start.button("Start run", type="primary", disabled=handle is not None):
    if not tasks_path().exists():
        st.error(f"Tasks file {tasks_path()} does not exist.")
    else:
        try:
            proc.start(
                workdir=workdir,
                config_path=config_path(),
                tasks_path=tasks_path(),
                auto_resume=int(auto_resume),
            )
            st.success("Run started.")
            time.sleep(0.5)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not start run: {exc}")

if btn_stop.button("Stop (graceful)", disabled=handle is None):
    if proc.stop(workdir):
        st.info("SIGINT sent. Orchestrator will drain in-flight tasks.")
    else:
        st.warning("Nothing to stop.")

if btn_kill.button("Force kill", disabled=handle is None):
    if proc.kill(workdir):
        st.warning("Force-killed.")
    else:
        st.warning("Nothing to kill.")

if btn_refresh.button("Refresh now"):
    st.rerun()

auto_refresh = st.toggle("Auto-refresh every 2s", value=handle is not None)

st.divider()

# ---- status table ---------------------------------------------------------

st.subheader("Task status")

specs = []
if tasks_path().exists():
    try:
        specs = parse_file(str(tasks_path()))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not parse tasks file: {exc}")

db = state_db_path()
executions: dict = {}
if db and db.exists():
    try:
        store = StateStore(db)
        executions = store.load_executions()
        store.close()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read state DB: {exc}")


def _row(spec, ex):
    now = datetime.now(timezone.utc)
    if ex is None:
        return {
            "id": spec.id,
            "title": spec.title,
            "status": "pending",
            "backend": "—",
            "attempts": 0,
            "reviews": 0,
            "elapsed": "—",
            "last_error": "",
        }
    if ex.started_at and not ex.finished_at:
        elapsed = _fmt((now - ex.started_at).total_seconds())
    elif ex.started_at and ex.finished_at:
        elapsed = _fmt((ex.finished_at - ex.started_at).total_seconds())
    else:
        elapsed = "—"
    return {
        "id": spec.id,
        "title": spec.title,
        "status": ex.status.value,
        "backend": ex.backend.value if ex.backend else "—",
        "attempts": ex.attempts,
        "reviews": ex.review_iterations,
        "elapsed": elapsed,
        "last_error": (ex.last_error or "")[:120],
    }


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


if specs:
    rows = [_row(s, executions.get(s.id)) for s in specs]
    df = pd.DataFrame(rows)

    def _style_status(val: str):
        color_map = {
            "done": "#1f7a3a",
            "failed": "#a4262c",
            "blocked": "#a4262c",
            "ready": "#0b6bcb",
            "pending": "#666",
            "planning": "#b58105",
            "coding": "#b58105",
            "reviewing": "#b58105",
            "fixing": "#b58105",
            "planned": "#0b6bcb",
            "coded": "#0b6bcb",
        }
        color = color_map.get(val, "#444")
        return f"background-color: {color}; color: white; font-weight: 600; text-align: center;"

    styled = df.style.map(_style_status, subset=["status"])
    st.dataframe(styled, use_container_width=True, hide_index=True)
else:
    st.info("No tasks parsed.")

st.divider()

# ---- live log tail --------------------------------------------------------

st.subheader("Live log tail")
log_text = proc.tail_log(workdir, max_bytes=200_000)
if log_text:
    st.code(log_text, language="log", line_numbers=False)
else:
    st.caption("No log output yet (the log appears once the run starts writing).")

# ---- task detail (expander) -----------------------------------------------

st.divider()
st.subheader("Task detail")
if specs:
    options = [s.id for s in specs]
    pick = st.selectbox("Inspect a task", options=options, index=0)
    ex = executions.get(pick)
    spec = next((s for s in specs if s.id == pick), None)
    if spec is not None:
        st.markdown(f"**{spec.title}**  ·  complexity `{spec.complexity.value}`")
        if spec.description:
            st.markdown(spec.description)
    if ex is None:
        st.caption("No execution row yet.")
    else:
        cols = st.columns(4)
        cols[0].metric("status", ex.status.value)
        cols[1].metric("backend", ex.backend.value if ex.backend else "—")
        cols[2].metric("attempts", ex.attempts)
        cols[3].metric("reviews", ex.review_iterations)

        if ex.last_error:
            st.error(ex.last_error)
        if ex.plan:
            with st.expander("Plan output"):
                st.write({
                    "approach": ex.plan.approach,
                    "files_to_modify": ex.plan.files_to_modify,
                    "files_to_create": ex.plan.files_to_create,
                    "test_strategy": ex.plan.test_strategy,
                    "estimated_loc": ex.plan.estimated_loc,
                })
        if ex.code:
            with st.expander("Code output"):
                st.write({
                    "summary": ex.code.summary,
                    "branch": ex.code.branch,
                    "tests_passed": ex.code.tests_passed,
                    "files_changed": [
                        {"path": c.path, "op": c.operation} for c in ex.code.files_changed
                    ],
                })
                if ex.code.test_output:
                    st.code(ex.code.test_output[-4000:], language="bash")
        if ex.review:
            with st.expander("Review output"):
                st.write({
                    "verdict": ex.review.verdict.value,
                    "score": ex.review.score,
                    "issues": ex.review.issues,
                    "auto_fixed": ex.review.auto_fixed,
                })

# ---- gentle auto-rerun ----------------------------------------------------

# Trust the toggle. The previous guard required a UI-launched run handle,
# which silently killed refresh for CLI-launched runs and for users who
# just wanted to watch live state arrive.
if auto_refresh:
    time.sleep(2)
    st.rerun()
