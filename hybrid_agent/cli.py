"""CLI entry point.

Commands:
  run    — parse a task list and execute the pipeline
  status — show current state of all tasks
  show   — show details of one task
  reset  — reset failed/blocked tasks back to ready (for retry)
  merge  — merge all done-task sandboxes into one output directory
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import AppConfig
from .logging_config import setup_logging
from .models import TaskStatus
from .orchestrator import Orchestrator
from .parsers import parse_file, supported_extensions
from .retry import retry_async
from .state import StateStore
from .task_generator import generate_tasks_md, load_skill_prompt, next_task_id

# Windows consoles default to cp1252, which chokes on the Unicode glyphs
# (→ — ⚠ …) we use in status messages and Rich tables. Force UTF-8 so Rich's
# legacy-Windows renderer can write them. Safe on POSIX too.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

console = Console()


def _setup_logging(cfg: AppConfig) -> None:
    """Configure logging from the loaded AppConfig."""
    setup_logging(
        level=cfg.log_level,
        log_file=cfg.log_file,
        json_file=cfg.log_json_file,
        console=console,
    )


@click.group()
def cli() -> None:
    """Hybrid Coding Agent — Claude planner/reviewer + local worker."""


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--tasks",
    "-t",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help=f"Task list file. Supported: {', '.join(supported_extensions())}",
)
@click.option(
    "--auto-resume",
    default=0,
    show_default=True,
    type=click.IntRange(min=0, max=20),
    help=(
        "After the run finishes, if any tasks are FAILED/BLOCKED, reset them "
        "and re-run, up to N extra rounds. Stops early when all DONE or no "
        "progress is made between rounds."
    ),
)
def run(config: str, tasks: str, auto_resume: int) -> None:
    """Run the pipeline over a task list."""
    cfg = AppConfig.load(config)
    _setup_logging(cfg)
    log = logging.getLogger("hybrid_agent")

    specs = parse_file(tasks)
    log.info("Parsed %d task(s) from %s", len(specs), tasks)
    if not specs:
        console.print("[red]No tasks parsed.[/red]")
        raise SystemExit(1)

    # Duplicate-id check: the orchestrator dedupes via {id: spec}, which
    # would silently drop tasks. Be loud about it.
    seen: dict[str, int] = {}
    for s in specs:
        seen[s.id] = seen.get(s.id, 0) + 1
    dups = sorted([tid for tid, n in seen.items() if n > 1])
    if dups:
        console.print(
            f"[red]Error:[/red] duplicate task ids in {tasks}: {dups}. Each task id must be unique."
        )
        raise SystemExit(2)

    db_path = cfg.state_db_path_resolved()
    log.info("State DB: %s", db_path)
    state = StateStore(db_path)
    try:
        prev_done = -1
        max_rounds = 1 + auto_resume
        for round_n in range(1, max_rounds + 1):
            if round_n > 1:
                # Reset FAILED + BLOCKED so the next orchestrator run picks
                # them back up. Orchestrator.initialize_state then auto-
                # promotes BLOCKED-with-deps-DONE to READY.
                executions = state.load_executions()
                resets = [
                    tid
                    for tid, ex in executions.items()
                    if ex.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
                ]
                if not resets:
                    log.info("Auto-resume: nothing to reset; finishing.")
                    break
                for tid in resets:
                    ex = executions[tid]
                    ex.status = TaskStatus.READY
                    ex.last_error = ""
                    asyncio.run(state.save_execution(ex))
                log.info(
                    "Auto-resume round %d/%d: reset %d task(s) (%s)",
                    round_n,
                    max_rounds,
                    len(resets),
                    ", ".join(sorted(resets)),
                )

            orch = Orchestrator(cfg, specs, state)
            asyncio.run(orch.run())

            executions = state.load_executions()
            done = sum(1 for ex in executions.values() if ex.status == TaskStatus.DONE)
            stuck = sum(
                1
                for ex in executions.values()
                if ex.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
            )
            if stuck == 0:
                log.info("All tasks reached DONE after %d round(s).", round_n)
                break
            if round_n >= max_rounds:
                log.warning(
                    "Auto-resume exhausted %d round(s); %d task(s) still FAILED/BLOCKED.",
                    max_rounds,
                    stuck,
                )
                break
            if done == prev_done:
                # Same set of FAILED/BLOCKED keeps coming back — bail rather
                # than burn budget on a permanent failure.
                log.warning(
                    "Auto-resume stopping: round %d made no progress (done=%d, stuck=%d).",
                    round_n,
                    done,
                    stuck,
                )
                break
            prev_done = done
    finally:
        state.close()


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
def status(config: str) -> None:
    """Show current task statuses from the state store."""
    cfg = AppConfig.load(config)
    state = StateStore(cfg.state_db_path_resolved())
    try:
        executions = state.load_executions()
    finally:
        state.close()

    if not executions:
        console.print("[yellow]No executions in state store yet.[/yellow]")
        return

    table = Table(title="Task status")
    table.add_column("ID", style="cyan")
    table.add_column("Status")
    table.add_column("Backend")
    table.add_column("Attempts")
    table.add_column("Reviews")
    table.add_column("Last error", style="red", max_width=40)

    color = {
        TaskStatus.DONE.value: "green",
        TaskStatus.FAILED.value: "red",
        TaskStatus.BLOCKED.value: "yellow",
    }
    for tid in sorted(executions):
        ex = executions[tid]
        c = color.get(ex.status.value, "white")
        table.add_row(
            tid,
            f"[{c}]{ex.status.value}[/{c}]",
            ex.backend.value if ex.backend else "-",
            str(ex.attempts),
            str(ex.review_iterations),
            (ex.last_error or "")[:80],
        )
    console.print(table)


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.argument("task_id")
def show(config: str, task_id: str) -> None:
    """Show details of one task."""
    cfg = AppConfig.load(config)
    state = StateStore(cfg.state_db_path_resolved())
    try:
        ex = state.get_execution(task_id)
    finally:
        state.close()

    if ex is None:
        console.print(f"[red]No execution for {task_id!r}[/red]")
        raise SystemExit(1)
    console.print_json(ex.model_dump_json(indent=2))


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--task-id",
    "task_ids",
    multiple=True,
    help="Specific task(s) to reset. Default: all FAILED + BLOCKED.",
)
def reset(config: str, task_ids: tuple[str, ...]) -> None:
    """Reset failed/blocked tasks back to READY for retry."""
    cfg = AppConfig.load(config)
    state = StateStore(cfg.state_db_path_resolved())
    try:
        executions = state.load_executions()
        targets = (
            list(task_ids)
            if task_ids
            else [
                tid
                for tid, ex in executions.items()
                if ex.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
            ]
        )
        if not targets:
            console.print("[yellow]Nothing to reset.[/yellow]")
            return
        for tid in targets:
            ex = executions.get(tid)
            if not ex:
                console.print(f"[red]Skip {tid}: not found[/red]")
                continue
            ex.status = TaskStatus.READY
            ex.last_error = ""
            asyncio.run(state.save_execution(ex))
            console.print(f"Reset [cyan]{tid}[/cyan] -> {ex.status.value}")
    finally:
        state.close()


@cli.command(name="generate-tasks")
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--feature",
    "-f",
    help="Feature description as a string. Use @path/to/file.txt to load from a file.",
)
@click.option(
    "--srs",
    type=click.Path(exists=True, dir_okay=False),
    help="Optional SRS file (.md / .txt / .rst / .docx) to ground tasks in.",
)
@click.option(
    "--output",
    "-o",
    default="tasks.md",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to write the generated tasks.md.",
)
@click.option(
    "--print-prompt",
    is_flag=True,
    help="Print the skill prompt and exit (no LLM call, no config needed).",
)
@click.option(
    "--append",
    "-a",
    is_flag=True,
    help=(
        "Append to an existing tasks file at --output instead of overwriting. "
        "New task IDs continue the existing series (e.g. T022 after T021)."
    ),
)
@click.option(
    "--update",
    "update_target",
    metavar="TASK_ID",
    default=None,
    help=(
        "Generate migration tasks that update TASK_ID's output. New tasks "
        "depend_on TASK_ID and are tagged 'migration'; the orchestrator "
        "branches their sandbox from TASK_ID's branch so the worker sees "
        "and can edit the old code. Implies --append."
    ),
)
def generate_tasks(
    config: str,
    feature: str | None,
    srs: str | None,
    output: str,
    print_prompt: bool,
    append: bool,
    update_target: str | None,
) -> None:
    """Generate a tasks.md from a feature request and/or SRS file."""
    if print_prompt:
        click.echo(load_skill_prompt())
        return

    if not feature:
        raise click.UsageError("--feature is required (or use --print-prompt)")

    # --update implies --append (migration tasks only make sense layered on top
    # of an existing series — there's nothing to update otherwise).
    if update_target:
        append = True

    # @file shortcut
    if feature.startswith("@"):
        fpath = Path(feature[1:]).expanduser()
        if not fpath.is_file():
            raise click.UsageError(f"--feature points to non-existent file: {fpath}")
        feature_text = fpath.read_text(encoding="utf-8")
    else:
        feature_text = feature

    cfg = AppConfig.load(config)
    _setup_logging(cfg)
    log = logging.getLogger("hybrid_agent")

    out_path = Path(output)

    # Load existing specs in append mode so we can compute the next id and
    # surface them to the model.
    existing_specs = []
    starting_id: str | None = None
    if append:
        if not out_path.is_file():
            console.print(
                f"[yellow]--append:[/yellow] {out_path} doesn't exist yet; "
                "creating fresh."
            )
        else:
            try:
                existing_specs = parse_file(out_path)
            except Exception as exc:
                raise click.UsageError(
                    f"--append: cannot parse existing {out_path}: {exc}"
                ) from exc
            starting_id = next_task_id(existing_specs)
            log.info(
                "Append mode: %d existing task(s) loaded; new ids start at %s",
                len(existing_specs),
                starting_id,
            )

    if update_target:
        existing_ids = {s.id for s in existing_specs}
        if update_target not in existing_ids:
            raise click.UsageError(
                f"--update {update_target}: id not found in {out_path}. "
                f"Known ids: {sorted(existing_ids) or '(none)'}"
            )
        log.info("Migration mode: new tasks will depend on %s", update_target)

    srs_path = Path(srs) if srs else None
    if srs_path:
        log.info("Grounding in SRS: %s", srs_path)

    log.info("Calling Claude to generate tasks…")
    content = asyncio.run(
        generate_tasks_md(
            cfg,
            feature=feature_text,
            srs_path=srs_path,
            existing_specs=existing_specs or None,
            starting_id=starting_id,
            update_target=update_target,
        )
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if append and out_path.is_file():
        # Concat existing + new with a clear separator so future re-parses
        # see one contiguous tasks.md and humans can find what was added.
        prior = out_path.read_text(encoding="utf-8").rstrip() + "\n\n"
        sep_label = (
            f"appended via generate-tasks --update {update_target}"
            if update_target
            else "appended via generate-tasks --append"
        )
        out_path.write_text(
            prior + f"<!-- {sep_label} -->\n\n" + content.lstrip(),
            encoding="utf-8",
        )
    else:
        out_path.write_text(content, encoding="utf-8")

    # Validate by re-parsing the *full* file (catches id collisions early).
    try:
        specs = parse_file(out_path)
    except Exception as exc:
        console.print(f"[yellow]⚠[/yellow]  Wrote {out_path} but parsing failed: {exc}")
        console.print("  Inspect the file and edit manually before running.")
        return

    # Surface id collisions: when appending, the model sometimes ignores the
    # starting_id directive and reuses old ids. Better to fail loudly than
    # let the orchestrator silently dedupe.
    seen: dict[str, int] = {}
    for s in specs:
        seen[s.id] = seen.get(s.id, 0) + 1
    dups = sorted([tid for tid, n in seen.items() if n > 1])
    if dups:
        console.print(
            f"[red]⚠ duplicate task ids after generation: {dups}[/red]\n"
            f"  Edit {out_path} to renumber the new tasks before running."
        )

    if append:
        new_specs = [s for s in specs if s.id not in {e.id for e in existing_specs}]
        title = (
            f"Appended {len(new_specs)} new task(s); total now {len(specs)} → {out_path}"
        )
        rows = new_specs
    else:
        title = f"Generated {len(specs)} task(s) → {out_path}"
        rows = specs

    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Tags")
    table.add_column("Cmplx")
    table.add_column("Deps")
    for s in rows:
        table.add_row(
            s.id,
            s.title[:50],
            ", ".join(s.tags) or "-",
            s.complexity.value,
            ", ".join(s.depends_on) or "-",
        )
    console.print(table)


_PKG_LINE_RE = None  # lazily compiled inside _reconcile_requirements_txt


def _reconcile_requirements_txt(
    versions: list[tuple[str, bytes]],
) -> tuple[str | None, list[str]]:
    """Union pip requirements lines across task versions.

    - Same package with conflicting version specs across tasks: keep the
      topo-latest spec, warn.
    - Non-package lines (urls, ``-e .``, ``-r other.txt``) are kept once each
      in original order.
    - Output is alphabetical packages, then leftover lines.
    """
    import re

    pkg_re = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*([<>=!~].*)?$")
    warnings: list[str] = []
    seen_pkg: dict[str, tuple[str, str]] = {}
    other_lines: list[str] = []

    for tid, content in versions:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"requirements from {tid}: not utf-8, skipping")
            continue
        for raw in text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = pkg_re.match(line)
            if m and m.group(2) is not None or (m and m.group(2) is None and "/" not in stripped):
                pkg = m.group(1).lower()
                if pkg in seen_pkg:
                    prev_line, prev_tid = seen_pkg[pkg]
                    if prev_line.strip() != line.strip():
                        warnings.append(
                            f"deps conflict {pkg}: {prev_tid}={prev_line.strip()!r} "
                            f"vs {tid}={line.strip()!r} -> kept {tid}"
                        )
                seen_pkg[pkg] = (line.strip(), tid)
            else:
                if line.strip() not in other_lines:
                    other_lines.append(line.strip())

    out_lines = sorted([ln for ln, _ in seen_pkg.values()], key=str.lower)
    out_lines.extend(other_lines)
    if not out_lines:
        return None, warnings
    return "\n".join(out_lines) + "\n", warnings


def _reconcile_env(versions: list[tuple[str, bytes]]) -> tuple[str | None, list[str]]:
    """Union ``KEY=VALUE`` lines across .env-style files.

    Topo-latest value wins on conflict (same as pure-copy behavior, but no
    keys get dropped). Comments and blank lines are not preserved — the
    output is sorted by key for deterministic diffs.
    """
    warnings: list[str] = []
    seen: dict[str, tuple[str, str]] = {}

    for tid, content in versions:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f".env from {tid}: not utf-8, skipping")
            continue
        for raw in text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if not key:
                continue
            if key in seen:
                prev_line, prev_tid = seen[key]
                if prev_line.strip() != line.strip():
                    warnings.append(
                        f"env conflict {key}: {prev_tid}={prev_line.strip()!r} "
                        f"vs {tid}={line.strip()!r} -> kept {tid}"
                    )
            seen[key] = (line.strip(), tid)

    if not seen:
        return None, warnings
    out_lines = [line for line, _ in sorted(seen.values(), key=lambda kv: kv[0].split("=", 1)[0])]
    return "\n".join(out_lines) + "\n", warnings


def _reconcile_init_py(versions: list[tuple[str, bytes]]) -> tuple[str | None, list[str]]:
    """Union ``__init__.py`` import statements and ``__all__`` lists.

    Skips reconcile (returns None) when any version has non-import body
    statements — auto-merging arbitrary code is unsafe.
    """
    import ast

    warnings: list[str] = []
    seen_imports: list[str] = []
    seen_imports_set: set[str] = set()
    dunder_all: set[str] = set()
    complex_tids: list[str] = []

    for tid, content in versions:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"__init__ from {tid}: not utf-8, skipping reconcile")
            return None, warnings
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            warnings.append(f"__init__ from {tid}: syntax error ({exc.msg}), skipping reconcile")
            return None, warnings

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                src = ast.get_source_segment(text, node) or ""
                if src and src not in seen_imports_set:
                    seen_imports_set.add(src)
                    seen_imports.append(src)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        dunder_all.add(elt.value)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                # Module docstring or string literal — harmless; skip.
                continue
            else:
                if tid not in complex_tids:
                    complex_tids.append(tid)

    if complex_tids:
        warnings.append(
            f"__init__ has non-import body in tasks {complex_tids}; skipping reconcile"
        )
        return None, warnings

    out_lines = list(seen_imports)
    if dunder_all:
        out_lines.append("")
        out_lines.append("__all__ = [")
        for item in sorted(dunder_all):
            out_lines.append(f'    "{item}",')
        out_lines.append("]")

    if not out_lines:
        return None, warnings
    return "\n".join(out_lines) + "\n", warnings


def _check_pyproject_deps(
    versions: list[tuple[str, bytes]],
) -> tuple[str | None, list[str]]:
    """Warn-only: report PEP 621 ``[project]`` dependency conflicts across tasks.

    Does NOT rewrite the file — TOML auto-merge would risk clobbering other
    config the user owns.
    """
    try:
        import tomllib  # type: ignore[unresolved-import]
    except ImportError:
        return None, ["pyproject.toml check skipped: tomllib unavailable (need Python 3.11+)"]
    import re

    warnings: list[str] = []
    seen: dict[str, tuple[str, str]] = {}
    dep_re = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$")

    for tid, content in versions:
        try:
            data = tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            warnings.append(f"pyproject from {tid}: parse error {exc}")
            continue
        proj = data.get("project") or {}
        deps: list[str] = list(proj.get("dependencies") or [])
        for items in (proj.get("optional-dependencies") or {}).values():
            deps.extend(items or [])
        for raw in deps:
            if not isinstance(raw, str):
                continue
            m = dep_re.match(raw.strip())
            if not m:
                continue
            name = m.group(1).lower()
            spec = m.group(2).strip()
            if name in seen:
                prev_spec, prev_tid = seen[name]
                if prev_spec != spec:
                    warnings.append(
                        f"pyproject deps conflict {name}: {prev_tid}={prev_spec!r} "
                        f"vs {tid}={spec!r} (no auto-rewrite)"
                    )
            seen[name] = (spec, tid)

    return None, warnings


# Mirrors the SKIP set in `merge` so we don't try to embed node_modules,
# .git, .venv, etc. when scanning for fix-touched files.
_REVIEW_SKIP_DIRS = {
    "__pycache__", ".hybrid_agent_sandboxes", ".git", "node_modules",
    ".next", ".nuxt", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache",
    "target", ".gradle", ".idea", ".vscode", "coverage", ".coverage",
    "htmlcov",
}


def _files_modified_since(out_dir: "Path", since_ts: float) -> set[str]:
    """Walk ``out_dir`` and return relative paths of files with mtime ≥ ``since_ts``.

    Used between fix-iterations to discover files the fix CREATED or
    modified — including single-author new files like ``tailwind.config.ts``
    that the original conflict-driven embedding would miss.
    """
    found: set[str] = set()
    if not out_dir.is_dir():
        return found
    for f in out_dir.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(out_dir)
        except ValueError:
            continue
        if any(p in _REVIEW_SKIP_DIRS for p in rel.parts):
            continue
        try:
            if f.stat().st_mtime >= since_ts:
                found.add(str(rel))
        except OSError:
            continue
    return found


def _print_review(label: str, verdict: str, body: str) -> None:
    """Pretty-print one review result inside the merge gate output."""
    if verdict == "PASS":
        console.print(f"[green]OK[/green] llm review ({label}) verdict: PASS")
    elif verdict == "WARN":
        console.print(f"[yellow]WARN[/yellow] llm review ({label}) verdict: WARN")
    else:
        console.print(f"[red]FAIL[/red] llm review ({label}) verdict: FAIL")
    console.print(f"[dim]--- llm review ({label}) ---[/dim]")
    console.print(body)
    console.print("[dim]--- end review ---[/dim]")


def _extract_patch_json(text: str) -> tuple[list[dict], str | None]:
    """Pull a JSON array out of a ``<patch>...</patch>`` block.

    Returns ``(ops, error_msg)``. Falls back to looking for a top-level JSON
    array if the explicit tag is missing — some models drop it. ``error_msg``
    is non-None when nothing parseable was found, or when the JSON itself
    is malformed.
    """
    import json
    import re

    m = re.search(r"<patch>\s*(\[.*?\])\s*</patch>", text, re.DOTALL)
    if not m:
        # Fallback: greedy match for any top-level JSON array.
        m = re.search(r"(\[\s*\{.*\}\s*\])\s*$", text, re.DOTALL)
    if not m:
        return [], "no <patch> block or trailing JSON array found in output"

    raw = m.group(1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"patch JSON malformed: {exc}"
    if not isinstance(parsed, list):
        return [], f"patch root must be a list, got {type(parsed).__name__}"
    return parsed, None


def _apply_patch(
    out_dir: "Path", ops: list[dict]
) -> tuple[list[str], list[str]]:
    """Apply a list of edit/write/delete ops deterministically.

    Returns ``(applied_paths, errors)``. Each op is one of:
      - ``{"op": "edit", "path": ..., "find": ..., "replace": ...}`` — the
        ``find`` string must appear EXACTLY ONCE in the target file.
      - ``{"op": "write", "path": ..., "content": ...}`` — overwrite or
        create from scratch.
      - ``{"op": "delete", "path": ...}`` — remove the file.

    Path traversal is blocked: ops targeting a path that resolves outside
    ``out_dir`` are skipped with an error.
    """
    applied: list[str] = []
    errors: list[str] = []
    out_root = out_dir.resolve()

    for i, op_dict in enumerate(ops):
        if not isinstance(op_dict, dict):
            errors.append(f"op #{i}: not an object")
            continue
        op = str(op_dict.get("op", "")).lower()
        rel_path = str(op_dict.get("path", "")).replace("\\", "/").lstrip("/")
        if not rel_path:
            errors.append(f"op #{i}: missing 'path'")
            continue

        target = (out_dir / rel_path).resolve()
        try:
            target.relative_to(out_root)
        except ValueError:
            errors.append(f"{rel_path}: refuses to write outside merged tree")
            continue

        if op == "edit":
            find = op_dict.get("find", "")
            replace = op_dict.get("replace", "")
            if not isinstance(find, str) or not isinstance(replace, str):
                errors.append(f"{rel_path}: edit find/replace must be strings")
                continue
            if not target.is_file():
                errors.append(f"{rel_path}: edit target not found")
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                errors.append(f"{rel_path}: read failed: {exc}")
                continue
            if not find:
                errors.append(f"{rel_path}: empty 'find' string")
                continue
            occurrences = content.count(find)
            if occurrences == 0:
                errors.append(f"{rel_path}: 'find' string not present")
                continue
            if occurrences > 1:
                errors.append(
                    f"{rel_path}: 'find' string matches {occurrences} times — "
                    f"include more context to make it unique"
                )
                continue
            new_content = content.replace(find, replace, 1)
            try:
                target.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{rel_path}: write failed: {exc}")
                continue
            applied.append(f"edit  {rel_path}")
        elif op == "write":
            content = op_dict.get("content", "")
            if not isinstance(content, str):
                errors.append(f"{rel_path}: write content must be string")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except OSError as exc:
                errors.append(f"{rel_path}: write failed: {exc}")
                continue
            applied.append(f"write {rel_path}")
        elif op == "delete":
            if not target.is_file():
                errors.append(f"{rel_path}: delete target not found")
                continue
            try:
                target.unlink()
            except OSError as exc:
                errors.append(f"{rel_path}: delete failed: {exc}")
                continue
            applied.append(f"del   {rel_path}")
        else:
            errors.append(f"{rel_path}: unknown op {op!r}")

    return applied, errors


async def _run_llm_fix_async(
    out_dir: "Path",
    review_body: str,
    file_history: dict[str, list[tuple[str, bytes]]],
    tasks_file: "Path",
    cfg: AppConfig,
    *,
    mode: str = "patch",
    extra_files: set[str] | None = None,
) -> str:
    """Ask Claude to fix the issues that the reviewer flagged.

    ``mode``:
      - ``"patch"`` (default): embed conflict files in the prompt, ask for a
        JSON patch in a ``<patch>...</patch>`` block, apply deterministically
        in Python. **No tool use** — sidesteps the Windows Claude CLI
        subprocess flake that crashes long-running tool queries (~3 min mark).
      - ``"tools"``: the legacy tools-based fix. Claude gets Read/Edit/
        Write/Grep/Glob over the merged tree. More flexible but unreliable
        on Windows for big merged trees.

    Returns a markdown summary of what was applied (and what failed). Raises
    on total LLM failure (the CLI surfaces as warning or hard fail per
    its policy).
    """
    if mode == "patch":
        return await _run_llm_fix_patch(
            out_dir, review_body, file_history, tasks_file, cfg,
            extra_files=extra_files,
        )
    if mode == "tools":
        return await _run_llm_fix_tools(out_dir, review_body, tasks_file, cfg)
    raise ValueError(f"unknown llm fix mode: {mode!r}")


async def _run_llm_fix_patch(
    out_dir: "Path",
    review_body: str,
    file_history: dict[str, list[tuple[str, bytes]]],
    tasks_file: "Path",
    cfg: AppConfig,
    *,
    extra_files: set[str] | None,
) -> str:
    """Patch-mode fix: no tool use, JSON patch round-trip."""
    from .config import RetryConfig
    from .llm import build_claude_pool

    pool = build_claude_pool(cfg)
    if pool is None:
        raise RuntimeError("claude.enabled=false in config — --llm-fix needs Claude.")

    try:
        tasks_text = tasks_file.read_text(encoding="utf-8")
    except OSError:
        tasks_text = ""
    if len(tasks_text) > 30_000:
        tasks_text = tasks_text[:30_000] + "\n\n... (truncated)"

    # Embed multi-author files + extras (same shape as inline review).
    per_file_cap = 8_000
    total_cap = 120_000
    paths_to_embed: list[str] = []
    seen_paths: set[str] = set()
    for rel, history in sorted(file_history.items()):
        if len(history) >= 2:
            paths_to_embed.append(rel)
            seen_paths.add(rel)
    if extra_files:
        for rel in sorted(extra_files):
            if rel not in seen_paths:
                paths_to_embed.append(rel)
                seen_paths.add(rel)

    embedded_blocks: list[str] = []
    embedded_chars = 0
    for rel in paths_to_embed:
        full = out_dir / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > per_file_cap:
            text = text[:per_file_cap] + "\n... (file truncated)"
        chunk = f"### {rel}\n```\n{text}\n```\n"
        if embedded_chars + len(chunk) > total_cap:
            continue
        embedded_blocks.append(chunk)
        embedded_chars += len(chunk)
    files_block = "\n".join(embedded_blocks) if embedded_blocks else "(no files embedded)"

    system_prompt = (
        "You are a senior engineer fixing integration issues found by a code "
        "reviewer in a multi-task merged tree. The user has shared the "
        "current contents of every relevant file with you below.\n\n"
        "You DO NOT have any tool access. Output your fixes as a single JSON "
        "patch in a `<patch>...</patch>` block at the end of your response.\n\n"
        "Patch schema (a JSON array of operation objects):\n"
        "  [\n"
        '    {"op": "edit", "path": "rel/path", "find": "<exact existing text>", '
        '"replace": "<new text>"},\n'
        '    {"op": "write", "path": "rel/path", "content": "<full file content>"},\n'
        '    {"op": "delete", "path": "rel/path"}\n'
        "  ]\n\n"
        "Rules:\n"
        "  1. Fix every [CRITICAL] issue. They block the project from running.\n"
        "  2. Fix [HIGH] issues when the correct fix is unambiguous from the "
        "spec the issue references. If a design call is required, pick the "
        "option that matches the spec/acceptance criteria.\n"
        "  3. Skip [LOW] issues unless they're a one or two line fix.\n"
        "  4. For \"edit\" ops, the `find` string MUST appear exactly once in "
        "the target file — include enough surrounding context (a unique "
        "function signature or import line) to make the match unambiguous.\n"
        "  5. For \"write\" ops, supply the COMPLETE new file content, not a "
        "diff — useful for new files (tailwind.config.ts) or full rewrites.\n"
        "  6. JSON strings: escape newlines as \\n, double-quotes as \\\", "
        "backslashes as \\\\.\n"
        "  7. Use forward slashes in paths regardless of OS.\n"
        "  8. Don't fabricate files you weren't shown — only edit/write paths "
        "you've seen in the embedded files block, or new sibling files (configs).\n"
        "  9. Don't introduce dependencies the issues didn't ask for.\n\n"
        "Before the `<patch>` block, briefly explain (one or two sentences "
        "per op) what each edit does and why. The CLI extracts only the "
        "JSON; your prose is for the human reading the merge log.\n"
    )

    user_prompt = (
        "## Reviewer's issues to fix\n\n"
        f"{review_body}\n\n"
        "## Project spec (tasks.md)\n\n"
        f"{tasks_text}\n\n"
        "## Current file contents in the merged tree\n\n"
        f"{files_block}\n\n"
        "## Your task\n\n"
        "Produce the JSON patch that resolves the issues. No tools."
    )

    fix_retry = RetryConfig(
        max_attempts=2,
        initial_delay_seconds=5.0,
        max_delay_seconds=30.0,
        exponential_base=2.0,
        jitter=True,
    )
    per_call_timeout_s = 240  # patch generation is fast; no tool round-trips

    async def _call():
        async with pool.acquire() as client:
            return await asyncio.wait_for(
                client.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    cwd=None,
                    # Empty list falls back to config default in ClaudeClient,
                    # so pass minimal harmless tools — system prompt forbids
                    # tool use anyway and Read alone won't trigger the long-
                    # running subprocess crash.
                    allowed_tools=["Read"],
                ),
                timeout=per_call_timeout_s,
            )

    result = await retry_async(_call, config=fix_retry, op_name="merge_llm_fix")
    text = (result.text or "").strip()

    ops, parse_err = _extract_patch_json(text)
    summary_lines: list[str] = []

    # Show the model's prose (everything before the patch block) for context.
    pre_patch = text.split("<patch>", 1)[0].strip()
    if pre_patch:
        summary_lines.append(pre_patch)
        summary_lines.append("")

    if parse_err is not None:
        summary_lines.append(f"## Patch parse error\n- {parse_err}")
        summary_lines.append(
            "\n_(model output didn't parse — no edits applied; see prose "
            "above for what it intended)_"
        )
        return "\n".join(summary_lines)

    applied, errors = _apply_patch(out_dir, ops)
    summary_lines.append(f"## Patch applied ({len(applied)}/{len(ops)} ops)")
    if applied:
        for line in applied:
            summary_lines.append(f"- {line}")
    else:
        summary_lines.append("- (no operations succeeded)")
    if errors:
        summary_lines.append("\n## Errors")
        for e in errors:
            summary_lines.append(f"- {e}")
    return "\n".join(summary_lines)


async def _run_llm_fix_tools(
    out_dir: "Path",
    review_body: str,
    tasks_file: "Path",
    cfg: AppConfig,
) -> str:
    """Legacy tools-based fix. Kept as opt-in via --llm-fix-mode tools.

    Prone to the Windows Claude CLI subprocess flake on big merged trees;
    use ``patch`` mode unless you specifically need Claude to grep around.
    """
    from .config import RetryConfig
    from .llm import build_claude_pool

    pool = build_claude_pool(cfg)
    if pool is None:
        raise RuntimeError("claude.enabled=false in config — --llm-fix needs Claude.")

    try:
        tasks_text = tasks_file.read_text(encoding="utf-8")
    except OSError:
        tasks_text = ""
    if len(tasks_text) > 30_000:
        tasks_text = tasks_text[:30_000] + "\n\n... (truncated)"

    system_prompt = (
        "You are a senior engineer fixing integration issues found by a "
        "code reviewer in a multi-task merged tree. Apply minimal, targeted "
        "edits that resolve the flagged issues without introducing new "
        "bugs.\n\n"
        "You have Read/Edit/Write/Grep/Glob over the merged output (your "
        "cwd). You DO NOT have Bash. If a fix needs a new dependency, edit "
        "the package manifest only; the user runs install themselves.\n\n"
        "Rules:\n"
        "  1. Fix every [CRITICAL] issue.\n"
        "  2. Fix [HIGH] issues when unambiguous; pick the spec-aligned "
        "option for ambiguous ones, leave a brief comment.\n"
        "  3. Skip [LOW] unless one or two lines.\n"
        "  4. Don't refactor unrelated code.\n"
        "  5. Re-read affected files after editing to verify no syntax errors.\n\n"
        "Emit a markdown summary at the end:\n\n"
        "## Files changed\n"
        "- path: description\n\n"
        "## Skipped\n"
        "- issue: reason\n\n"
        "Do NOT emit a Verdict line."
    )
    user_prompt = (
        "## Reviewer's issues to fix\n\n"
        f"{review_body}\n\n"
        "## Project spec (tasks.md)\n\n"
        f"{tasks_text}\n\n"
        "## Your task\n\n"
        "Cwd is the merged tree. Apply minimal fixes and emit the summary."
    )

    fix_retry = RetryConfig(
        max_attempts=2, initial_delay_seconds=10.0, max_delay_seconds=60.0,
        exponential_base=2.0, jitter=True,
    )
    per_call_timeout_s = 600

    async def _call():
        async with pool.acquire() as client:
            return await asyncio.wait_for(
                client.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    cwd=out_dir,
                    allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"],
                ),
                timeout=per_call_timeout_s,
            )

    result = await retry_async(_call, config=fix_retry, op_name="merge_llm_fix")
    return (result.text or "").strip()


def _run_llm_review_with_fallback(
    out_dir: "Path",
    file_history: dict[str, list[tuple[str, bytes]]],
    tasks_file: "Path",
    cfg: AppConfig,
    *,
    mode: str,
    allow_fallback: bool,
    extra_files: set[str] | None = None,
) -> tuple[str | None, str]:
    """Sync wrapper around _run_llm_review_async with tools→inline fallback.

    Returns ``(verdict, body)``. On total failure, ``verdict`` is ``None`` and
    ``body`` is the last error message — the caller surfaces it as a warning.

    Fallback semantics:
      - ``mode="tools"`` + ``allow_fallback=True``: if tools mode raises (the
        Windows Claude-CLI subprocess flake is the typical cause), retry once
        in inline mode and tag the body. The user's exhausted his choice of
        thoroughness; better to come back with a passable inline review than
        to lose the gate entirely.
      - ``mode="inline"`` or ``allow_fallback=False``: surface the failure
        as-is.

    ``extra_files`` is passed through to inline-mode calls (tools mode reads
    the disk so doesn't need it).
    """
    console.print(f"[dim]running LLM review (Claude, mode={mode})…[/dim]")
    try:
        return asyncio.run(
            _run_llm_review_async(
                out_dir, file_history, tasks_file, cfg,
                mode=mode, extra_files=extra_files,
            )
        )
    except Exception as exc:  # noqa: BLE001
        first_err = f"{type(exc).__name__}: {exc}"
        if mode != "tools" or not allow_fallback:
            return None, first_err
        console.print(
            f"[yellow]tools mode failed ({first_err}); "
            f"falling back to inline mode…[/yellow]"
        )
        try:
            verdict, body = asyncio.run(
                _run_llm_review_async(
                    out_dir, file_history, tasks_file, cfg,
                    mode="inline", extra_files=extra_files,
                )
            )
        except Exception as exc2:  # noqa: BLE001
            return None, (
                f"both modes failed — tools: {first_err}; "
                f"inline: {type(exc2).__name__}: {exc2}"
            )
        body = (
            "_(fallback: tools mode flaked, this review came from inline mode)_\n\n"
            + body
        )
        return verdict, body


async def _run_llm_review_async(
    out_dir: "Path",
    file_history: dict[str, list[tuple[str, bytes]]],
    tasks_file: "Path",
    cfg: AppConfig,
    *,
    mode: str = "inline",
    extra_files: set[str] | None = None,
) -> tuple[str, str]:
    """Ask Claude to semantically review the merged tree.

    ``mode``:
      - ``"inline"`` (default): pre-extract conflict files into the prompt
        and instruct Claude not to call tools. Faster, deterministic, sized
        to fit. Recommended on Windows where the Claude SDK subprocess
        occasionally crashes mid-stream during tool-heavy queries.
      - ``"tools"``: pass ``cwd=out_dir`` and allow Read/Grep/Glob so the
        model can explore beyond the conflict set. More thorough, but the
        long-running subprocess pattern is what triggers the
        ``Command failed with exit code 1`` flake we've seen in the wild.

    ``extra_files`` (inline mode only): paths to embed in addition to the
    multi-author conflict files. Used by the post-fix review to surface
    files the fix CREATED or modified that aren't in the original conflict
    set (e.g. ``tailwind.config.ts`` written from scratch by the fix).

    Returns (verdict, body). Verdict is one of "PASS"/"WARN"/"FAIL" extracted
    from the model's first ``Verdict:`` line; body is the full markdown
    review for printing. Defaults to ``WARN`` if the model didn't emit a
    parseable verdict line — better to surface the body than silently
    treat ambiguous output as a pass.
    """
    import re

    from .config import RetryConfig
    from .llm import build_claude_pool

    if mode not in ("inline", "tools"):
        raise ValueError(f"unknown llm review mode: {mode!r}")

    pool = build_claude_pool(cfg)
    if pool is None:
        raise RuntimeError(
            "claude.enabled=false in config — LLM review needs Claude. "
            "Either enable Claude or drop --llm-review."
        )

    # Build a compact conflict log: only files written by 2+ tasks, with the
    # full chain of authors. This is the signal Claude needs most — it tells
    # it where integration drift is most likely.
    conflict_lines: list[str] = []
    for rel, history in sorted(file_history.items()):
        if len(history) < 2:
            continue
        chain = " -> ".join(tid for tid, _ in history)
        conflict_lines.append(f"- {rel}: {chain}")
    conflict_block = "\n".join(conflict_lines) if conflict_lines else "(none)"

    # tasks.md is the spec — what the project SHOULD do. Cap to keep the
    # prompt within budget.
    try:
        tasks_text = tasks_file.read_text(encoding="utf-8")
    except OSError as exc:
        tasks_text = f"(could not read {tasks_file}: {exc})"
    if len(tasks_text) > 30_000:
        tasks_text = tasks_text[:30_000] + "\n\n... (truncated)"

    base_focus = (
        "Focus on:\n"
        "  1. Stale references — code that imports/calls a name whose "
        "signature, location, or existence changed in a later task.\n"
        "  2. Logic conflicts — same function/component defined two "
        "different ways across tasks, where the topo-late version dropped "
        "behavior the earlier version had.\n"
        "  3. Missing wiring — a feature described in tasks.md that has "
        "files but isn't actually hooked up (e.g. route registered but no "
        "handler, component imported but never rendered).\n"
        "  4. Cross-cutting drift — README, env, route configs, public API "
        "shape that disagrees with the code.\n\n"
    )
    output_format = (
        "Output format (strict — the CLI parses this):\n"
        "  Verdict: PASS | WARN | FAIL\n"
        "    - PASS  = nothing broken; ship it\n"
        "    - WARN  = issues exist but project will run; fix when convenient\n"
        "    - FAIL  = integration is broken (won't run, regressed feature, "
        "missing critical wiring)\n"
        "  Then a markdown bulleted list of issues, each prefixed by "
        "[CRITICAL]/[HIGH]/[LOW] and citing a file path with line if known.\n"
        "If clean, write a single line `Verdict: PASS` and `No issues found.`"
    )

    if mode == "inline":
        # Pre-extract every conflict file so Claude doesn't need to call
        # tools. Cap each file and the total to stay well inside context.
        per_file_cap = 8_000
        total_cap = 120_000

        # Build the unified embed set: multi-author files first (the original
        # conflict set), then any extras supplied by the caller (typically
        # files the fix pass touched that weren't already in file_history).
        paths_to_embed: list[tuple[str, str]] = []  # (rel_path, label_suffix)
        seen_paths: set[str] = set()
        for rel, history in sorted(file_history.items()):
            if len(history) < 2:
                continue
            paths_to_embed.append((rel, ""))
            seen_paths.add(rel)
        if extra_files:
            for rel in sorted(extra_files):
                if rel in seen_paths:
                    continue
                paths_to_embed.append((rel, " (fix-touched)"))
                seen_paths.add(rel)

        embedded_blocks: list[str] = []
        embedded_chars = 0
        skipped_for_size = 0
        for rel, label in paths_to_embed:
            full = out_dir / rel
            if not full.is_file():
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > per_file_cap:
                text = text[:per_file_cap] + "\n... (file truncated)"
            chunk = f"### {rel}{label}\n```\n{text}\n```\n"
            if embedded_chars + len(chunk) > total_cap:
                skipped_for_size += 1
                continue
            embedded_blocks.append(chunk)
            embedded_chars += len(chunk)
        if skipped_for_size:
            embedded_blocks.append(
                f"\n_({skipped_for_size} additional file(s) elided to fit "
                f"context — review remaining files based on conflict log "
                f"alone or rerun with --llm-review-mode tools.)_\n"
            )
        files_block = "\n".join(embedded_blocks) if embedded_blocks else "(no files to embed)"

        system_prompt = (
            "You are a senior reviewer auditing a multi-task code merge. The "
            "project was decomposed into tasks T0XX, each producing a sandbox; "
            "files were merged in topological dependency order, so later "
            "tasks' versions of a file overwrote earlier ones. Your job is "
            "to find integration drift the per-task review couldn't see.\n\n"
            "Every conflict file's content has been extracted for you below. "
            "DO NOT call any tools (Read/Grep/Glob) — work purely from the "
            "embedded text. If you need information that isn't there, say "
            "so explicitly in your review rather than guessing.\n\n"
            + base_focus
            + output_format
        )
        user_prompt = (
            "## Project spec (tasks.md)\n\n"
            f"{tasks_text}\n\n"
            "## File-history conflicts (multi-author files in topo order)\n\n"
            f"{conflict_block}\n\n"
            "## Conflict file contents (extracted — do NOT Read again)\n\n"
            f"{files_block}\n\n"
            "## Your task\n\n"
            "Cross-reference the embedded files against tasks.md and the "
            "conflict log. Emit Verdict + issue list per the format. No tools."
        )
        cwd_arg: Path | None = None
        # Pass empty allowed_tools as a final guardrail. ClaudeClient's
        # ``allowed_tools or self.config.allowed_tools`` would fall back to
        # config, but the system-prompt instruction is the actual enforcement.
        allowed_tools_arg: list[str] = ["Read"]  # minimal harmless fallback
    else:  # mode == "tools"
        system_prompt = (
            "You are a senior reviewer auditing a multi-task code merge. The "
            "project was decomposed into tasks T0XX, each producing a "
            "sandbox; files were merged in topological dependency order. "
            "You have read-only tool access (Read, Grep, Glob) to the merged "
            "output directory. Use them sparingly to ground your review.\n\n"
            "On Windows the Claude SDK subprocess can flake on long tool-"
            "heavy queries — keep your investigation focused. Read the "
            "conflict files first; only Grep/Glob when you have a specific "
            "hypothesis.\n\n"
            + base_focus
            + output_format
        )
        user_prompt = (
            "## Project spec (tasks.md)\n\n"
            f"{tasks_text}\n\n"
            "## File-history conflicts (multi-author files in topo order)\n\n"
            f"{conflict_block}\n\n"
            "## Your task\n\n"
            "The merged output is your cwd. Read the conflict files, "
            "correlate against the spec, emit Verdict + issues."
        )
        cwd_arg = out_dir
        allowed_tools_arg = ["Read", "Grep", "Glob"]

    # Tighter retry/timeout than the global config: the LLM review is a
    # single one-shot call, not a critical path. Burning 10 minutes on a
    # transient subprocess flake (5 retries × 2 min) is worse than failing
    # fast and printing a warning.
    review_retry = RetryConfig(
        max_attempts=2,
        initial_delay_seconds=5.0,
        max_delay_seconds=30.0,
        exponential_base=2.0,
        jitter=True,
    )
    per_call_timeout_s = 180  # cap each attempt; tool-mode tends to dawdle

    async def _call():
        async with pool.acquire() as client:
            return await asyncio.wait_for(
                client.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    cwd=cwd_arg,
                    allowed_tools=allowed_tools_arg,
                ),
                timeout=per_call_timeout_s,
            )

    result = await retry_async(_call, config=review_retry, op_name="merge_llm_review")
    text = (result.text or "").strip()

    m = re.search(r"^\s*Verdict:\s*(PASS|WARN|FAIL)\b", text, re.IGNORECASE | re.MULTILINE)
    verdict = m.group(1).upper() if m else "WARN"
    return verdict, text


def _detect_system_test_command(out_dir: "Path") -> str | None:
    """Probe ``out_dir`` for a runnable integration/e2e/system test command.

    Order of preference (first match wins):
      1. ``tests/integration``  -> ``pytest tests/integration -x --tb=short``
      2. ``tests/e2e``          -> ``pytest tests/e2e -x --tb=short``
      3. ``tests/system``       -> ``pytest tests/system -x --tb=short``
      4. ``integration_tests``  -> ``pytest integration_tests -x --tb=short``
      5. ``e2e/`` + ``package.json`` script ``test:e2e``/``e2e``/``integration``
         -> ``npm run <script> --silent``
      6. ``Makefile`` with a ``test-system`` or ``e2e`` target
         -> ``make <target>``

    Returns None when nothing matches — caller decides whether that's a hard
    fail (rare) or a skip (default).
    """
    import json

    pytest_dirs = [
        ("tests/integration", "pytest tests/integration -x --tb=short"),
        ("tests/e2e", "pytest tests/e2e -x --tb=short"),
        ("tests/system", "pytest tests/system -x --tb=short"),
        ("integration_tests", "pytest integration_tests -x --tb=short"),
    ]
    for sub, cmd in pytest_dirs:
        if (out_dir / sub).is_dir():
            return cmd

    if (out_dir / "e2e").is_dir() and (out_dir / "package.json").is_file():
        try:
            data = json.loads((out_dir / "package.json").read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            for name in ("test:e2e", "e2e", "integration"):
                if name in scripts:
                    return f"npm run {name} --silent"
        except (OSError, ValueError):
            pass

    makefile = out_dir / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
            for target in ("test-system", "system-test", "e2e", "test-e2e", "integration"):
                # Crude but adequate: look for "<target>:" at the start of a line.
                if any(
                    ln.strip().startswith(f"{target}:") for ln in text.splitlines()
                ):
                    return f"make {target}"
        except OSError:
            pass

    return None


def _reconcile_cross_cutting(
    out_dir: "Path",
    file_history: dict[str, list[tuple[str, bytes]]],
) -> tuple[list[str], list[str]]:
    """Run all reconcilers over multi-version files. Rewrites ``out_dir`` in place.

    Returns (rewrites, warnings) — both are pre-formatted strings ready to print.
    """
    import fnmatch
    import os

    rules: list[tuple[str, str, callable]] = [  # (label, basename pattern, fn)
        ("requirements", "requirements*.txt", _reconcile_requirements_txt),
        ("env", ".env", _reconcile_env),
        ("env", ".env.example", _reconcile_env),
        ("env", ".env.*", _reconcile_env),
        ("init", "__init__.py", _reconcile_init_py),
        ("pyproject", "pyproject.toml", _check_pyproject_deps),
    ]

    rewrites: list[str] = []
    warnings: list[str] = []

    for rel, history in file_history.items():
        if len(history) < 2:
            continue  # nothing to reconcile if only one task wrote it
        base = os.path.basename(rel)
        for label, pattern, fn in rules:
            if not fnmatch.fnmatchcase(base, pattern):
                continue
            new_content, warns = fn(history)
            for w in warns:
                warnings.append(f"{rel}: {w}")
            if new_content is not None:
                target = out_dir / rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(new_content, encoding="utf-8")
                    rewrites.append(
                        f"{rel}: reconciled {label} from {len(history)} task version(s)"
                    )
                except OSError as exc:
                    warnings.append(f"{rel}: failed to write reconciled file: {exc}")
            break

    return rewrites, warnings


def _merge_review_gate(
    out_dir: "Path",
    file_history: dict[str, list[tuple[str, bytes]]],
    cfg: AppConfig,
    *,
    tasks_file: "Path | None" = None,
    llm_review: bool = False,
    llm_review_mode: str = "inline",
    llm_review_fallback: bool = True,
    llm_fix: bool = False,
    llm_fix_iterations: int = 3,
    llm_fix_mode: str = "patch",
) -> int:
    """Post-merge sanity gate. Returns 0 on pass, non-zero on hard failure.

    Tier D (deterministic reconcile, runs first so static checks see the
    reconciled output): union requirements*.txt, .env*, and __init__.py
    across task versions; warn-only on pyproject.toml dep conflicts.
    Tier A (static):
      1. content-drift: when task B overwrites task A's version of the same
         file with non-trivially different content, report how many "meaningful"
         lines from A no longer appear in B. False-positive on rewrites is
         expected — this is a hint, not a hard fail.
      2. syntax check `.py` files in the merged output. Hard fail.
    Tier B (LLM semantic review, only when ``llm_review=True``): one Claude
    call grounded in tasks.md + the conflict log + read-only access to the
    merged tree. Returns a Verdict + issue list. ``FAIL`` is a hard fail;
    ``WARN`` prints but doesn't block.
    Tier C (tests): run the project's test command on `out_dir`. Hard fail
    on FAILED/TIMEOUT/ERROR; soft on NO_TESTS unless `require_tests=true`.
    Tier F (system test): run the integration/e2e/system test command — set
    via ``orchestrator.system_test_command`` or auto-detected from
    ``tests/integration``, ``tests/e2e``, ``tests/system``,
    ``integration_tests``, or ``e2e/`` with package.json. Hard fail on
    FAILED/TIMEOUT/ERROR; skip silently when nothing is configured or
    detected (the project simply doesn't have a system test yet).
    """
    import asyncio
    import difflib
    import py_compile
    import tempfile
    from pathlib import Path

    from .language import detect_language
    from .test_runner import TestStatus, run_tests

    console.print()
    console.print("[bold]Running merge review gate…[/bold]")

    hard_fails = 0
    drift_warnings: list[str] = []

    # ---- D. Reconcile cross-cutting files ------------------------------------
    rewrites, recon_warnings = _reconcile_cross_cutting(out_dir, file_history)
    if rewrites:
        console.print(f"[green]OK[/green] reconciled {len(rewrites)} cross-cutting file(s):")
        for r in rewrites:
            console.print(f"  {r}")
    if recon_warnings:
        console.print(f"[yellow]WARN reconcile produced {len(recon_warnings)} note(s):[/yellow]")
        for w in recon_warnings:
            console.print(f"  {w}")
    if not rewrites and not recon_warnings:
        console.print("[dim]reconcile: nothing to merge across tasks[/dim]")

    # ---- A.1 content-drift across overwrites ---------------------------------
    # Heuristic: compare the last two versions of any multi-version file.
    # Lines unique to the older version (after stripping whitespace/blank/import
    # noise) are the ones at risk of having been silently dropped.
    NOISY_PREFIXES = ("import ", "from ", "#", '"""', "'''")
    for rel, history in file_history.items():
        if len(history) < 2:
            continue
        prev_tid, prev_bytes = history[-2]
        cur_tid, cur_bytes = history[-1]
        if prev_bytes == cur_bytes:
            continue

        try:
            prev_text = prev_bytes.decode("utf-8")
            cur_text = cur_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary file — can't diff

        def _meaningful(lines: list[str]) -> set[str]:
            return {
                ln.strip()
                for ln in lines
                if ln.strip() and not ln.strip().startswith(NOISY_PREFIXES)
            }

        prev_set = _meaningful(prev_text.splitlines())
        cur_set = _meaningful(cur_text.splitlines())
        if not prev_set:
            continue
        dropped = prev_set - cur_set
        # Only warn when the drop is sizeable — small refactors are noisy.
        if len(dropped) >= 5 and len(dropped) / len(prev_set) >= 0.3:
            ratio = difflib.SequenceMatcher(None, prev_text, cur_text).ratio()
            drift_warnings.append(
                f"{rel}: {prev_tid} -> {cur_tid} dropped {len(dropped)} of "
                f"{len(prev_set)} meaningful lines (similarity={ratio:.2f})"
            )

    if drift_warnings:
        console.print(
            f"[yellow]WARN content drift on {len(drift_warnings)} file(s):[/yellow]"
        )
        for w in drift_warnings:
            console.print(f"  {w}")
        console.print(
            "  [dim]These are hints, not hard failures. Inspect to confirm "
            "the rewrite was intentional.[/dim]"
        )
    else:
        console.print("[green]OK[/green] no content-drift warnings")

    # ---- A.2 syntax-check .py files ------------------------------------------
    py_failures: list[tuple[str, str]] = []
    for rel in file_history:
        if not rel.endswith(".py"):
            continue
        full = out_dir / rel
        if not full.is_file():
            continue
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pyc") as tf:
                cfile = tf.name
            py_compile.compile(str(full), cfile=cfile, doraise=True)
            Path(cfile).unlink(missing_ok=True)
        except py_compile.PyCompileError as exc:
            py_failures.append((rel, str(exc)))
        except OSError as exc:
            py_failures.append((rel, f"compile io error: {exc}"))

    if py_failures:
        hard_fails += 1
        console.print(f"[red]FAIL syntax errors in {len(py_failures)} .py file(s):[/red]")
        for rel, msg in py_failures:
            console.print(f"  [red]{rel}[/red]: {msg.splitlines()[0][:200]}")
    else:
        py_count = sum(1 for r in file_history if r.endswith(".py"))
        if py_count:
            console.print(f"[green]OK[/green] py_compile clean on {py_count} .py file(s)")

    # ---- B. LLM semantic review (opt-in, costs money) -----------------------
    if llm_review:
        if tasks_file is None:
            console.print("[yellow]SKIP llm review:[/yellow] tasks_file not provided")
        else:
            import time

            verdict, body = _run_llm_review_with_fallback(
                out_dir, file_history, tasks_file, cfg,
                mode=llm_review_mode,
                allow_fallback=llm_review_fallback,
            )
            if verdict is None:
                # Initial review couldn't even run. Don't block on flaky
                # infra; deterministic gates already passed.
                console.print(f"[yellow]WARN llm review failed:[/yellow] {body}")
            else:
                _print_review("initial", verdict, body)

                # Multi-iteration fix loop. Each round: if FAIL and budget
                # remains, run fix → re-review with fix-touched files
                # embedded → loop. Stop when verdict ≠ FAIL, budget runs
                # out, or fix/review crashes.
                extra_files: set[str] = set()
                fix_iter = 0
                fix_aborted = False
                while (
                    llm_fix
                    and verdict == "FAIL"
                    and fix_iter < llm_fix_iterations
                    and not fix_aborted
                ):
                    fix_iter += 1
                    console.print(
                        f"[yellow]Verdict FAIL — running --llm-fix "
                        f"iteration {fix_iter}/{llm_fix_iterations}…[/yellow]"
                    )
                    before_ts = time.time()
                    try:
                        fix_summary = asyncio.run(
                            _run_llm_fix_async(
                                out_dir, body, file_history, tasks_file, cfg,
                                mode=llm_fix_mode,
                                extra_files=extra_files or None,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        console.print(
                            f"[red]FAIL llm fix iter {fix_iter} crashed:[/red] "
                            f"{type(exc).__name__}: {exc}"
                        )
                        hard_fails += 1
                        fix_aborted = True
                        break

                    console.print(f"[dim]--- llm fix summary (iter {fix_iter}) ---[/dim]")
                    console.print(fix_summary)
                    console.print("[dim]--- end fix summary ---[/dim]")

                    # Discover what fix actually touched. Includes new
                    # single-author files (tailwind config, etc.) that
                    # wouldn't otherwise appear in the embed set.
                    touched = _files_modified_since(out_dir, before_ts)
                    new_for_embed = touched - extra_files
                    extra_files |= touched
                    if touched:
                        console.print(
                            f"[dim]fix touched {len(touched)} file(s); "
                            f"{len(new_for_embed)} new in embed set[/dim]"
                        )

                    # Re-review (force inline + use embed set so model sees
                    # the fixed bytes AND any newly-created configs).
                    console.print(
                        f"[dim]re-running review after iter {fix_iter}…[/dim]"
                    )
                    verdict, body = _run_llm_review_with_fallback(
                        out_dir, file_history, tasks_file, cfg,
                        mode="inline",
                        allow_fallback=False,
                        extra_files=extra_files,
                    )
                    if verdict is None:
                        console.print(
                            f"[red]FAIL post-fix review (iter {fix_iter}) "
                            f"failed to run:[/red] {body}"
                        )
                        hard_fails += 1
                        fix_aborted = True
                        break
                    _print_review(f"after iter {fix_iter}", verdict, body)

                # Decide gate verdict from final state. Crash paths above
                # already incremented hard_fails and broke out.
                if not fix_aborted and verdict == "FAIL":
                    if llm_fix and fix_iter == llm_fix_iterations:
                        console.print(
                            f"[red]FAIL[/red] llm fix exhausted "
                            f"{llm_fix_iterations} iteration(s); verdict still FAIL"
                        )
                    hard_fails += 1

    # ---- C run tests on the merged tree --------------------------------------
    test_command = cfg.orchestrator.test_command
    if test_command is None:
        profile = detect_language(out_dir)
        test_command = profile.test_command if profile else None

    if not test_command:
        msg = "no test command (configured or auto-detected)"
        if cfg.orchestrator.require_tests:
            hard_fails += 1
            console.print(f"[red]FAIL tests:[/red] {msg} but require_tests=true")
        else:
            console.print(f"[yellow]SKIP tests:[/yellow] {msg}")
    else:
        console.print(f"[dim]test command:[/dim] {test_command}")
        result = asyncio.run(
            run_tests(
                out_dir,
                test_command,
                timeout_seconds=cfg.orchestrator.test_timeout_seconds,
            )
        )
        status = result.status
        if status == TestStatus.PASSED:
            console.print(
                f"[green]OK tests passed[/green] ({result.duration_s:.1f}s)"
            )
        elif status == TestStatus.NO_TESTS:
            if cfg.orchestrator.require_tests:
                hard_fails += 1
                console.print("[red]FAIL tests:[/red] no tests collected (require_tests=true)")
            else:
                console.print("[yellow]SKIP tests:[/yellow] no tests collected")
        else:
            hard_fails += 1
            console.print(
                f"[red]FAIL tests {status.value}[/red] (rc={result.rc}, "
                f"{result.duration_s:.1f}s)"
            )
            tail = result.output
            if tail:
                console.print("[dim]--- output tail ---[/dim]")
                console.print(tail)

    # ---- F. System / integration test on the merged tree --------------------
    sys_cmd = cfg.orchestrator.system_test_command or _detect_system_test_command(out_dir)
    if not sys_cmd:
        console.print(
            "[dim]system test: not configured and nothing detected "
            "(set orchestrator.system_test_command or add tests/integration|e2e)[/dim]"
        )
    else:
        console.print(f"[dim]system test:[/dim] {sys_cmd}")
        sys_result = asyncio.run(
            run_tests(
                out_dir,
                sys_cmd,
                timeout_seconds=cfg.orchestrator.system_test_timeout_seconds,
            )
        )
        sys_status = sys_result.status
        if sys_status == TestStatus.PASSED:
            console.print(
                f"[green]OK system test passed[/green] ({sys_result.duration_s:.1f}s)"
            )
        elif sys_status == TestStatus.NO_TESTS:
            # Configured / detected but no tests collected — the user pointed
            # us at an empty suite. Treat as soft so an empty integration dir
            # doesn't block ship.
            console.print("[yellow]SKIP system test:[/yellow] no tests collected")
        else:
            hard_fails += 1
            console.print(
                f"[red]FAIL system test {sys_status.value}[/red] "
                f"(rc={sys_result.rc}, {sys_result.duration_s:.1f}s)"
            )
            sys_tail = sys_result.output
            if sys_tail:
                console.print("[dim]--- system test output tail ---[/dim]")
                console.print(sys_tail)

    # ---- summary -------------------------------------------------------------
    console.print()
    if hard_fails:
        console.print(
            f"[bold red]Review gate FAILED — {hard_fails} hard failure(s).[/bold red]"
        )
        return 1
    if drift_warnings:
        console.print(
            "[bold yellow]Review gate PASSED with content-drift warnings.[/bold yellow]"
        )
    else:
        console.print("[bold green]Review gate PASSED.[/bold green]")
    return 0


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--output",
    "-o",
    default="merged",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Destination directory for merged output.",
)
@click.option(
    "--tasks",
    "-t",
    default="tasks.md",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option("--dry-run", is_flag=True, help="Print what would be copied without writing.")
@click.option(
    "--review",
    is_flag=True,
    help=(
        "Run a post-merge gate: flag overwrites that drop substantial content "
        "from earlier tasks, syntax-check .py files in the output, and run the "
        "project test command on the merged tree. Exits non-zero if any gate "
        "fails. Implies --dry-run is OFF."
    ),
)
@click.option(
    "--llm-review",
    is_flag=True,
    help=(
        "Add an LLM (Claude) semantic review pass to --review. Inspects the "
        "merged tree for stale references, missing wiring, and cross-task "
        "drift the deterministic gates can't catch. Costs ~one Claude call. "
        "Verdict FAIL is a hard fail; WARN is printed but non-blocking."
    ),
)
@click.option(
    "--llm-review-mode",
    type=click.Choice(["inline", "tools"], case_sensitive=False),
    default="inline",
    show_default=True,
    help=(
        "How --llm-review feeds context to Claude. 'inline' embeds conflict "
        "files in the prompt and forbids tool calls — fast, deterministic, "
        "and avoids the Windows Claude-CLI subprocess flake. 'tools' lets "
        "Claude explore the tree via Read/Grep/Glob — more thorough but "
        "prone to long-running subprocess crashes on big trees."
    ),
)
@click.option(
    "--no-llm-fallback",
    "llm_review_fallback",
    flag_value=False,
    default=True,
    help=(
        "Disable the auto-fallback from tools mode to inline mode when "
        "tools mode flakes. By default, if --llm-review-mode tools fails "
        "after retries, we transparently retry once in inline mode so the "
        "review still produces output."
    ),
)
@click.option(
    "--llm-fix",
    is_flag=True,
    help=(
        "When --llm-review verdict is FAIL, invoke Claude with Edit/Write "
        "access on the merged tree to fix the flagged issues in place, "
        "then re-run the review to verify. Loops up to --llm-fix-iterations "
        "rounds. Implies --llm-review. Each round costs ~one extra fix call "
        "+ one verification call. Hard-fails only when the loop exits with "
        "verdict still FAIL."
    ),
)
@click.option(
    "--llm-fix-iterations",
    type=click.IntRange(min=1, max=10),
    default=3,
    show_default=True,
    help=(
        "Max review/fix/review iterations when --llm-fix is on. Each round "
        "after the first reviews against the fix-touched files (including "
        "newly-created single-author files like tailwind configs). Loop "
        "stops early on PASS/WARN or fix crash."
    ),
)
@click.option(
    "--llm-fix-mode",
    type=click.Choice(["patch", "tools"], case_sensitive=False),
    default="patch",
    show_default=True,
    help=(
        "How --llm-fix applies edits. 'patch' (default) embeds files in the "
        "prompt and asks Claude for a JSON patch which the CLI applies "
        "deterministically -- no tool subprocess, dodges the Windows Claude-"
        "CLI flake. 'tools' lets Claude Edit/Write directly via the SDK; "
        "more flexible but unreliable on big trees on Windows."
    ),
)
@click.option(
    "--cleanup",
    is_flag=True,
    help=(
        "After a successful merge (and review gate, if --review is on), "
        "remove the per-task sandboxes and their `agent/<task_id>` git "
        "branches. Only touches tasks whose files made it into the merge — "
        "failed/skipped task sandboxes are left for debugging. Ignored "
        "when --dry-run is set."
    ),
)
def merge(
    config: str,
    output: str,
    tasks: str,
    dry_run: bool,
    review: bool,
    llm_review: bool,
    llm_review_mode: str,
    llm_review_fallback: bool,
    llm_fix: bool,
    llm_fix_iterations: int,
    llm_fix_mode: str,
    cleanup: bool,
) -> None:
    """Merge all DONE task sandboxes into one output directory.

    Files from later tasks in dependency order overwrite earlier ones,
    so the most integrated version of every file wins.
    Skips build artifacts and dependency caches: __pycache__, node_modules,
    .git, .next, .venv, dist, build, .pytest_cache, .mypy_cache, target,
    .gradle, .idea, .vscode, coverage. Plus .pyc/.pyo files.
    """
    import shutil
    from pathlib import Path

    cfg = AppConfig.load(config)
    # --llm-fix implies --llm-review (no point fixing without reviewing first).
    # --llm-review implies --review (it's a sub-gate of the review pipeline).
    if llm_fix:
        llm_review = True
    if llm_review:
        review = True
    if review:
        # Static + test gates need the merged tree on disk to inspect.
        # Silently bail rather than running the gates against a phantom output.
        if dry_run:
            console.print("[red]--review is incompatible with --dry-run[/red]")
            raise SystemExit(2)
        if llm_review and not cfg.claude.enabled:
            console.print(
                "[red]--llm-review requires claude.enabled=true in config.[/red]"
            )
            raise SystemExit(2)
        _setup_logging(cfg)
    state = StateStore(cfg.state_db_path_resolved())
    try:
        executions = state.load_executions()
    finally:
        state.close()

    specs = parse_file(tasks)
    from .dag import TaskDAG

    dag = TaskDAG(specs)
    topo = dag.topo_order()

    sandbox_base = Path(cfg.project_root_path()) / cfg.sandbox.base_dir
    out_dir = Path(output)

    # Directories that should never appear in the merged output. Sandboxes
    # accumulate these via `npm install`, `pip install -e .`, git worktree
    # bookkeeping, build steps, etc. — copying them out is both slow (one
    # `node_modules` is 25k+ files) and wrong (e.g. `.git` would clobber the
    # user's repo metadata at the destination).
    SKIP = {
        "__pycache__",
        ".hybrid_agent_sandboxes",
        ".git",
        "node_modules",
        ".next",
        ".nuxt",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        "target",  # rust/maven
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        ".coverage",
        "htmlcov",
    }
    SKIP_SUFFIXES = {".pyc", ".pyo"}

    copied: dict[str, str] = {}  # rel_path -> winning task
    skipped_tasks: list[str] = []
    merged_task_ids: list[str] = []  # tasks that actually contributed files
    # rel_path -> [(task_id, raw_bytes)] in topo order. Only populated when
    # --review is on, since holding every overwritten version in RAM is
    # wasteful for the common pure-copy case.
    file_history: dict[str, list[tuple[str, bytes]]] = {}

    for tid in topo:
        ex = executions.get(tid)
        if not ex or ex.status != TaskStatus.DONE:
            skipped_tasks.append(tid)
            continue

        sandbox = sandbox_base / tid
        if not sandbox.is_dir():
            console.print(f"[yellow]Warning:[/yellow] sandbox missing for {tid}: {sandbox}")
            continue

        merged_task_ids.append(tid)
        for src in sandbox.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(sandbox)
            parts = rel.parts
            if any(p in SKIP for p in parts):
                continue
            if src.suffix in SKIP_SUFFIXES:
                continue

            rel_str = str(rel)
            dest = out_dir / rel

            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

            if review:
                try:
                    file_history.setdefault(rel_str, []).append(
                        (tid, src.read_bytes())
                    )
                except OSError as exc:
                    console.print(f"[yellow]Warning:[/yellow] cannot read {src}: {exc}")

            prev = copied.get(rel_str)
            if prev:
                console.print(
                    f"  [yellow]conflict[/yellow] {rel_str}  {prev} -> [cyan]{tid}[/cyan] (kept {tid})"
                )
            else:
                console.print(f"  [green]copy[/green]     {rel_str}  from {tid}")
            copied[rel_str] = tid

    console.print()
    if skipped_tasks:
        console.print(f"[yellow]Skipped (not done):[/yellow] {', '.join(skipped_tasks)}")
    if dry_run:
        console.print(f"[bold]Dry run — {len(copied)} file(s) would be written to {output}[/bold]")
    else:
        console.print(f"[bold green]Merged {len(copied)} file(s) into {output}/[/bold green]")

    if review:
        rc = _merge_review_gate(
            out_dir,
            file_history,
            cfg,
            tasks_file=Path(tasks),
            llm_review=llm_review,
            llm_review_mode=llm_review_mode,
            llm_review_fallback=llm_review_fallback,
            llm_fix=llm_fix,
            llm_fix_iterations=llm_fix_iterations,
            llm_fix_mode=llm_fix_mode,
        )
        if rc != 0:
            raise SystemExit(rc)

    if cleanup and not dry_run and merged_task_ids:
        _cleanup_merged_sandboxes(cfg, merged_task_ids)


def _cleanup_merged_sandboxes(cfg: AppConfig, task_ids: list[str]) -> None:
    """Remove the worktree + branch for each merged task.

    Reuses ``SandboxManager.cleanup`` so the teardown path is identical to
    what the orchestrator would do on its own. Failures are logged but
    don't abort — the merge already succeeded; leftover state is annoying
    but not catastrophic.
    """
    from .sandbox import SandboxManager

    project_root = cfg.project_root_path()
    mgr = SandboxManager(project_root, cfg.sandbox)
    sandbox_base = (project_root / cfg.sandbox.base_dir).resolve()

    async def _run_all() -> None:
        for tid in task_ids:
            sandbox_path = sandbox_base / tid
            branch = f"{cfg.sandbox.branch_prefix}{tid}"
            try:
                await mgr.cleanup(sandbox_path, branch)
                console.print(f"  [dim]cleaned[/dim]  {tid}  (branch {branch})")
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"  [yellow]cleanup warning[/yellow] {tid}: {exc}"
                )

    asyncio.run(_run_all())
    console.print(
        f"[bold green]Cleaned {len(task_ids)} sandbox(es) + branches.[/bold green]"
    )


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--tasks",
    "-t",
    default="tasks.md",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help=f"Task list file. Supported: {', '.join(supported_extensions())}",
)
def tui(config: str, tasks: str) -> None:
    """Launch the interactive terminal UI."""
    from .tui import run_tui

    run_tui(Path(config), Path(tasks))


@cli.command()
@click.option(
    "--config",
    "-c",
    default="config.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--tasks",
    "-t",
    default="tasks.md",
    show_default=True,
    type=click.Path(dir_okay=False),
    help=(
        "Task list file shown in the UI. Doesn't need to exist — you can "
        "create one from inside the Tasks page."
    ),
)
@click.option(
    "--host",
    default="localhost",
    show_default=True,
    help="Bind address for Streamlit (use 0.0.0.0 to expose on the network).",
)
@click.option(
    "--port",
    default=8501,
    show_default=True,
    type=int,
    help="Port for the Streamlit server.",
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Do not auto-open a browser window.",
)
def webui(config: str, tasks: str, host: str, port: int, no_browser: bool) -> None:
    """Launch the Streamlit web UI (tasks editor, run dashboard, config, cost)."""
    import os
    import subprocess
    import sys
    from importlib import resources

    try:
        import streamlit  # noqa: F401
    except ImportError:
        console.print(
            "[red]streamlit is not installed.[/red] "
            "Install with: [cyan]pip install -e '.[webui]'[/cyan]"
        )
        raise SystemExit(2) from None

    app_path = resources.files("hybrid_agent.webui").joinpath("app.py")
    env = os.environ.copy()
    env["HYBRID_AGENT_WEBUI_CONFIG"] = str(Path(config).resolve())
    env["HYBRID_AGENT_WEBUI_TASKS"] = str(Path(tasks).resolve())

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true" if no_browser else "false",
        "--browser.gatherUsageStats",
        "false",
    ]
    console.print(f"[green]Launching webui on http://{host}:{port}[/green]")
    raise SystemExit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    cli()
