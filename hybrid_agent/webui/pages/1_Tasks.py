"""Tasks page — table editor + raw-markdown editor + import / export + generate."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# Streamlit launches each page as its own script; make the package importable
# regardless of CWD.
_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import pandas as pd
import streamlit as st

from hybrid_agent.models import Complexity, TaskSpec
from hybrid_agent.parsers import parse_file
from hybrid_agent.tui.tasks_io import dump_tasks, serialize_tasks
from hybrid_agent.webui import process as proc
from hybrid_agent.webui.shared import (
    config_path,
    load_config,
    project_root,
    setup_page,
    tasks_path,
)

setup_page("Tasks", icon="[T]")
st.title("Tasks")
st.caption(f"Editing `{tasks_path()}`")

if proc.read_handle(project_root()) is not None:
    st.warning("A run is in progress — changes here won't affect the running pipeline.")


def _load_specs() -> list[TaskSpec]:
    p = tasks_path()
    if not p.exists():
        return []
    try:
        return parse_file(str(p))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to parse {p.name}: {exc}")
        return []


def _to_df(specs: list[TaskSpec]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": s.id,
                "title": s.title,
                "complexity": s.complexity.value,
                "depends_on": ", ".join(s.depends_on),
                "tags": ", ".join(s.tags),
                "target_files": ", ".join(s.target_files),
                "acceptance_criteria": s.acceptance_criteria,
                "description": s.description,
            }
            for s in specs
        ]
    )


def _from_df(df: pd.DataFrame) -> list[TaskSpec]:
    out: list[TaskSpec] = []
    for _, row in df.iterrows():
        tid = str(row.get("id", "")).strip()
        if not tid:
            continue
        try:
            complexity = Complexity(str(row.get("complexity", "medium")).strip().lower())
        except ValueError:
            complexity = Complexity.MEDIUM
        out.append(
            TaskSpec(
                id=tid,
                title=str(row.get("title", "")).strip() or "(untitled)",
                complexity=complexity,
                depends_on=_split_csv(row.get("depends_on", "")),
                tags=_split_csv(row.get("tags", "")),
                target_files=_split_csv(row.get("target_files", "")),
                acceptance_criteria=str(row.get("acceptance_criteria", "")),
                description=str(row.get("description", "")),
            )
        )
    return out


def _split_csv(val: object) -> list[str]:
    if val is None:
        return []
    s = str(val).strip()
    if not s:
        return []
    return [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]


tab_table, tab_raw, tab_import, tab_generate = st.tabs(
    ["Table editor", "Raw markdown", "Import / Export", "Generate from request"]
)

# ---- Table editor ---------------------------------------------------------

with tab_table:
    specs = _load_specs()
    df = _to_df(specs)
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "id": st.column_config.TextColumn("ID", width="small", required=True),
            "title": st.column_config.TextColumn("Title", width="medium"),
            "complexity": st.column_config.SelectboxColumn(
                "Complexity",
                options=["low", "medium", "high"],
                width="small",
            ),
            "depends_on": st.column_config.TextColumn(
                "Depends on", help="Comma-separated task ids"
            ),
            "tags": st.column_config.TextColumn("Tags", help="Comma-separated"),
            "target_files": st.column_config.TextColumn(
                "Files", help="Comma-separated file paths"
            ),
            "acceptance_criteria": st.column_config.TextColumn(
                "Acceptance", width="large"
            ),
            "description": st.column_config.TextColumn(
                "Description (multi-line)", width="large"
            ),
        },
        key="tasks_table_editor",
    )

    btn_save, btn_revert, _ = st.columns([1, 1, 4])
    if btn_save.button("Save to tasks file", type="primary"):
        try:
            new_specs = _from_df(edited)
            ids = [s.id for s in new_specs]
            dups = [i for i in set(ids) if ids.count(i) > 1]
            if dups:
                st.error(f"Duplicate ids: {dups}. Each task id must be unique.")
            else:
                dump_tasks(tasks_path(), new_specs)
                st.success(f"Saved {len(new_specs)} task(s) to {tasks_path().name}.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Save failed: {exc}")
    if btn_revert.button("Reload from disk"):
        st.rerun()

# ---- Raw markdown ---------------------------------------------------------

with tab_raw:
    p = tasks_path()
    current = p.read_text(encoding="utf-8") if p.exists() else ""
    text = st.text_area(
        "tasks.md source",
        value=current,
        height=540,
        key="tasks_raw_editor",
    )
    save_col, fmt_col, _ = st.columns([1, 1, 4])
    if save_col.button("Save raw text", type="primary", key="raw_save"):
        try:
            # round-trip through parser to validate
            from hybrid_agent.parsers.markdown import parse_markdown_text

            parsed = parse_markdown_text(text)
            p.write_text(text, encoding="utf-8")
            st.success(f"Saved ({len(parsed)} task(s) parsed).")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Refusing to save — invalid markdown: {exc}")
    if fmt_col.button("Reformat", help="Re-serialize via canonical writer"):
        try:
            from hybrid_agent.parsers.markdown import parse_markdown_text

            parsed = parse_markdown_text(text)
            st.session_state["tasks_raw_editor"] = serialize_tasks(parsed)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Reformat failed: {exc}")

# ---- Import / Export ------------------------------------------------------

with tab_import:
    st.subheader("Import")
    st.caption(
        "Upload a tasks file in any supported format (.md, .yaml, .json, .csv, "
        ".xlsx, .docx). The file is parsed and overwrites the current tasks.md "
        "in canonical markdown form."
    )
    uploaded = st.file_uploader(
        "Choose a tasks file",
        type=["md", "yaml", "yml", "json", "csv", "xlsx", "docx"],
        key="tasks_uploader",
    )
    if uploaded is not None:
        suffix = Path(uploaded.name).suffix
        tmp = tasks_path().parent / f".upload_tmp{suffix}"
        tmp.write_bytes(uploaded.read())
        try:
            parsed = parse_file(str(tmp))
            st.info(f"Parsed {len(parsed)} task(s) from {uploaded.name}.")
            if st.button(f"Overwrite {tasks_path().name} with these tasks"):
                dump_tasks(tasks_path(), parsed)
                tmp.unlink(missing_ok=True)
                st.success("Imported.")
                st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not parse uploaded file: {exc}")
        finally:
            if tmp.exists():
                # leave the tmp for the import button click; cleaned on next run
                pass

    st.divider()
    st.subheader("Export")
    if tasks_path().exists():
        st.download_button(
            "Download tasks.md",
            data=tasks_path().read_bytes(),
            file_name=tasks_path().name,
            mime="text/markdown",
        )
    else:
        st.caption("No tasks file yet.")

# ---- Generate from request ------------------------------------------------

with tab_generate:
    st.subheader("Generate / add tasks from a natural-language request")
    st.caption(
        "Hand Claude a feature description (plus optional SRS document) and let "
        "it decompose the work into properly-formed tasks. Append to keep prior "
        "DONE tasks alive, or use Migrate to extend a specific task."
    )

    cfg_for_gen = load_config()
    if cfg_for_gen is None or not cfg_for_gen.claude.enabled:
        st.warning(
            "`claude.enabled` is false (or config not loaded). Task generation "
            "uses the Claude SDK — enable it on the Config page first."
        )

    current_specs = _load_specs()

    if proc.read_handle(project_root()) is not None:
        st.warning("A run is in progress — generation will queue behind it on disk writes.")

    mode = st.radio(
        "Mode",
        options=["New (overwrite tasks.md)", "Append (keep existing tasks)", "Migrate from existing task"],
        index=1 if current_specs else 0,
        horizontal=True,
        key="gen_mode",
    )

    update_target = None
    if mode.startswith("Migrate"):
        if not current_specs:
            st.info("Migrate mode needs at least one existing task.")
        else:
            update_target = st.selectbox(
                "Migrate from",
                options=[s.id for s in current_specs],
                help=(
                    "The new tasks will depend on this id, be tagged `migration`, "
                    "and fork their sandbox from that task's branch so they edit "
                    "its output in place."
                ),
            )

    feature_text = st.text_area(
        "Feature description / new requirement",
        height=180,
        placeholder=(
            "e.g. Add JWT authentication and a /me endpoint that returns the "
            "current user. Include rate limiting (10 req/min/IP) on /login."
        ),
        key="gen_feature",
    )

    srs_col, _ = st.columns([1, 2])
    srs_upload = srs_col.file_uploader(
        "Optional: ground the generation in an SRS document",
        type=["md", "markdown", "txt", "rst", "yaml", "yml", "json", "docx"],
        key="gen_srs",
        help="The SRS contents will be passed to Claude alongside the feature text.",
    )

    auto_run = st.checkbox(
        "Auto-start run after generation",
        value=False,
        help="If checked, launches `hybrid-agent run` immediately when generation succeeds.",
    )
    auto_resume_n = 0
    if auto_run:
        auto_resume_n = st.number_input(
            "auto-resume rounds (when auto-starting)",
            min_value=0,
            max_value=10,
            value=3,
        )

    gen_btn = st.button("Generate now", type="primary", disabled=not feature_text.strip())

    if gen_btn:
        if not feature_text.strip():
            st.error("Feature description is empty.")
        elif cfg_for_gen is None:
            st.error("Cannot generate: config missing.")
        elif not cfg_for_gen.claude.enabled:
            st.error("Cannot generate: claude.enabled is false in config.yaml.")
        else:
            srs_tmp_path: Path | None = None
            if srs_upload is not None:
                suffix = Path(srs_upload.name).suffix or ".txt"
                srs_tmp_path = tasks_path().parent / f".gen_srs_tmp{suffix}"
                srs_tmp_path.write_bytes(srs_upload.read())

            cmd: list[str] = [
                sys.executable,
                "-m",
                "hybrid_agent",
                "generate-tasks",
                "-c",
                str(config_path()),
                "-f",
                feature_text,
                "-o",
                str(tasks_path()),
            ]
            if srs_tmp_path is not None:
                cmd += ["--srs", str(srs_tmp_path)]
            if mode.startswith("Append"):
                cmd += ["--append"]
            elif mode.startswith("Migrate") and update_target:
                cmd += ["--update", update_target]
            # "New" mode: no extra flag — generate-tasks overwrites by default

            with st.spinner(
                "Asking Claude to decompose the request (typically 30-90s)…"
            ):
                started = time.time()
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=str(project_root()),
                        capture_output=True,
                        text=True,
                        timeout=900,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    st.error("Generation timed out after 15 minutes.")
                    result = None
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Generation failed to start: {exc}")
                    result = None
                finally:
                    if srs_tmp_path is not None and srs_tmp_path.exists():
                        srs_tmp_path.unlink(missing_ok=True)

            if result is not None:
                elapsed = time.time() - started
                if result.returncode == 0:
                    new_specs = _load_specs()
                    delta = len(new_specs) - len(current_specs)
                    st.success(
                        f"Generated in {elapsed:.0f}s. "
                        + (
                            f"Added {delta} new task(s); total {len(new_specs)}."
                            if delta > 0
                            else f"Tasks file now has {len(new_specs)} task(s)."
                        )
                    )
                    with st.expander("Generator stdout", expanded=False):
                        st.code(result.stdout or "(empty)", language="text")
                    with st.expander("Updated tasks.md preview", expanded=True):
                        st.code(
                            tasks_path().read_text(encoding="utf-8"),
                            language="markdown",
                        )
                    if auto_run:
                        try:
                            proc.start(
                                workdir=project_root(),
                                config_path=config_path(),
                                tasks_path=tasks_path(),
                                auto_resume=int(auto_resume_n),
                            )
                            st.success(
                                "Run started. Switch to the Run page to follow progress."
                            )
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Auto-start failed: {exc}")
                else:
                    st.error(f"generate-tasks failed (exit {result.returncode}).")
                    if result.stdout:
                        with st.expander("stdout", expanded=False):
                            st.code(result.stdout, language="text")
                    if result.stderr:
                        with st.expander("stderr", expanded=True):
                            st.code(result.stderr, language="text")
