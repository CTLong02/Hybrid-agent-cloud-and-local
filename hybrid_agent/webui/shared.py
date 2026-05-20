"""Shared helpers for every Streamlit page: paths, loaders, sidebar."""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
import yaml

from ..config import AppConfig

ENV_CONFIG = "HYBRID_AGENT_WEBUI_CONFIG"
ENV_TASKS = "HYBRID_AGENT_WEBUI_TASKS"


def config_path() -> Path:
    return Path(os.environ.get(ENV_CONFIG, "config.yaml")).resolve()


def tasks_path() -> Path:
    return Path(os.environ.get(ENV_TASKS, "tasks.md")).resolve()


def load_config_raw() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_config() -> AppConfig | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        return AppConfig.load(p)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not parse {p.name}: {exc}")
        return None


def project_root() -> Path:
    cfg = load_config()
    if cfg is None:
        return Path.cwd()
    return cfg.project_root_path()


def state_db_path() -> Path | None:
    cfg = load_config()
    if cfg is None:
        return None
    return cfg.state_db_path_resolved()


def setup_page(title: str, icon: str = "[H]") -> None:
    """Common page-level setup: title, layout, sidebar header."""
    st.set_page_config(
        page_title=f"Hybrid Agent — {title}",
        page_icon=icon,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    with st.sidebar:
        st.markdown("### Hybrid Agent")
        st.caption(f"config: `{config_path().name}`")
        st.caption(f"tasks:  `{tasks_path().name}`")
        st.divider()
