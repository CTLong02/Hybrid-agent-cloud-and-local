"""Streamlit entrypoint — Dashboard.

Run via the CLI:

    hybrid-agent webui -c config.yaml -t tasks.md

Or directly:

    HYBRID_AGENT_WEBUI_CONFIG=config.yaml \
    HYBRID_AGENT_WEBUI_TASKS=tasks.md \
    streamlit run hybrid_agent/webui/app.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Streamlit runs this file as a standalone script, not as a package module,
# so relative imports won't work. Make the package importable, then use
# absolute imports.
_PKG_ROOT = Path(__file__).resolve().parents[2]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

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

setup_page("Dashboard", icon="[H]")

st.title("Hybrid Coding Agent")
st.caption("Claude planner + reviewer · local Qwen worker · web dashboard")

cfg = load_config()
cfg_p = config_path()
tasks_p = tasks_path()

# --- top-level health line --------------------------------------------------

col_a, col_b, col_c, col_d = st.columns(4)

col_a.metric(
    "Config",
    "OK" if cfg is not None else "missing",
    delta=cfg_p.name,
    delta_color="off",
)

specs_count = 0
if tasks_p.exists():
    try:
        specs_count = len(parse_file(str(tasks_p)))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not parse {tasks_p.name}: {exc}")
col_b.metric("Tasks file", "OK" if tasks_p.exists() else "missing", delta=f"{specs_count} task(s)")

running_handle = proc.read_handle(project_root()) if cfg else None
col_c.metric("Run status", "RUNNING" if running_handle else "idle")

db = state_db_path()
status_summary = "—"
if db and db.exists():
    try:
        store = StateStore(db)
        counts = store.status_counts()
        store.close()
        if counts:
            done = counts.get(TaskStatus.DONE.value, 0)
            total = sum(counts.values())
            status_summary = f"{done}/{total} DONE"
    except Exception as exc:  # noqa: BLE001
        status_summary = f"err: {exc}"
col_d.metric("State DB", "present" if db and db.exists() else "absent", delta=status_summary)

st.divider()

# --- status counts pills ----------------------------------------------------

if db and db.exists():
    try:
        store = StateStore(db)
        counts = Counter(store.status_counts())
        store.close()
    except Exception:  # noqa: BLE001
        counts = Counter()
    if counts:
        st.subheader("Task status overview")
        order = [
            TaskStatus.DONE,
            TaskStatus.READY,
            TaskStatus.PENDING,
            TaskStatus.PLANNING,
            TaskStatus.CODING,
            TaskStatus.REVIEWING,
            TaskStatus.FIXING,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
        ]
        cols = st.columns(len(order))
        for col, status in zip(cols, order, strict=False):
            col.metric(status.value, counts.get(status.value, 0))

# --- navigation help --------------------------------------------------------

st.subheader("Pages")
left, right = st.columns(2)

with left:
    st.markdown(
        "- **Tasks** — view, edit, add, or import a `tasks.md`.\n"
        "- **Run** — start / stop the pipeline and follow live status."
    )

with right:
    st.markdown(
        "- **Config** — edit `config.yaml` (models, routing, cost caps, timeouts).\n"
        "- **Cost & Logs** — token spend per task / model + live log tail."
    )

st.divider()
st.caption(
    f"Working directory: `{project_root()}`  ·  "
    f"State DB: `{db}`  ·  "
    f"Use the sidebar to switch pages."
)
