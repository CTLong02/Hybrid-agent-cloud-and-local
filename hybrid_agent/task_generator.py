"""Task generator: turn a feature request (and optional SRS) into tasks.md.

Reads the bundled skill prompt at `prompts/task_generator.md`, builds a user
prompt from the inputs, and invokes the Claude pool with read-only tools so
Claude can ground itself in any provided codebase.

Post-processing: the model occasionally repeats its full task list (a known
failure mode under long context / temperature drift). `_dedupe_task_sections`
catches that and drops repeated `# Task: <id>` sections, keeping the first
occurrence of each id, so the resulting `tasks.md` always has unique ids.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import AppConfig
from .llm import build_claude_pool
from .models import TaskSpec
from .retry import retry_async

log = logging.getLogger(__name__)


PROMPT_FILE = Path(__file__).parent / "prompts" / "task_generator.md"


def load_skill_prompt() -> str:
    """Read the skill prompt as text."""
    return PROMPT_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SRS reading
# ---------------------------------------------------------------------------


def read_srs(path: Path) -> str:
    """Extract plain text from an SRS file.

    Supported: .md, .markdown, .txt, .rst, .yaml, .yml, .json, .docx.
    PDFs are not handled here — convert to .md/.txt first.
    """
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".docx":
        from docx import Document  # lazy import

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if suffix == ".pdf":
        raise ValueError(
            "PDF SRS not supported directly. Convert to .md/.txt/.docx first, "
            "e.g. `pdftotext srs.pdf srs.txt`."
        )
    # Best-effort fallback
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_ID_NUMERIC_RE = re.compile(r"^([A-Za-z]*?)(\d+)$")


def next_task_id(specs: list[TaskSpec]) -> str:
    """Return the next ID in the existing series, or ``T001`` if empty.

    Looks at the numeric suffix of every spec id and increments the maximum.
    Preserves the prefix of the highest-numbered id (so a project using
    ``TASK001`` keeps ``TASK###`` and one using ``T001`` keeps ``T###``).
    Width is preserved when all ids share the same width; otherwise we use
    3-digit padding as a sane default.
    """
    if not specs:
        return "T001"
    max_n = -1
    prefix = "T"
    width = 3
    for s in specs:
        m = _ID_NUMERIC_RE.match(s.id)
        if not m:
            continue
        n = int(m.group(2))
        if n > max_n:
            max_n = n
            prefix = m.group(1) or "T"
            width = max(3, len(m.group(2)))
    if max_n < 0:
        return "T001"
    return f"{prefix}{max_n + 1:0{width}d}"


def _summarize_existing(specs: list[TaskSpec], limit: int = 40) -> str:
    """Compact one-line summary per existing task, capped to keep the prompt short.

    Long acceptance criteria are dropped — we only need enough detail for the
    LLM to know what's already covered and pick correct ``depends_on`` for the
    new tasks.
    """
    if not specs:
        return ""
    lines: list[str] = []
    for s in specs[:limit]:
        deps = ",".join(s.depends_on) if s.depends_on else "-"
        title = (s.title or "").strip().replace("\n", " ")[:80]
        lines.append(f"- {s.id} (deps={deps}): {title}")
    if len(specs) > limit:
        lines.append(f"- ... ({len(specs) - limit} more existing tasks elided)")
    return "\n".join(lines)


def build_user_prompt(
    feature: str,
    srs_text: str | None = None,
    srs_name: str | None = None,
    existing_specs: list[TaskSpec] | None = None,
    starting_id: str | None = None,
    update_target: str | None = None,
) -> str:
    parts: list[str] = []

    if srs_text:
        # Cap SRS size to keep the prompt within context. ~60k chars ≈ 15k tokens.
        capped = (
            srs_text
            if len(srs_text) <= 60_000
            else (
                srs_text[:60_000] + "\n\n... (SRS truncated; ask about specific sections if needed)"
            )
        )
        header = f"## SRS document: {srs_name}" if srs_name else "## SRS document"
        parts.append(f"{header}\n\n{capped}\n")

    if existing_specs:
        parts.append(
            "## Existing tasks (already in tasks.md, do NOT regenerate)\n\n"
            + _summarize_existing(existing_specs)
            + "\n"
        )

    if starting_id:
        parts.append(
            "## ID assignment (strict)\n\n"
            f"You are appending NEW tasks to an existing `tasks.md`. Start ids "
            f"at **{starting_id}** and continue sequentially "
            f"({starting_id}, then the next number, etc.). Do NOT reuse any id "
            f"from the existing-tasks list. Do NOT restart from T001.\n"
        )

    if update_target:
        parts.append(
            "## Migration mode\n\n"
            f"The new requirement updates the work originally done by "
            f"**{update_target}**. Each new task you generate MUST:\n"
            f"  - have `depends_on` that includes `{update_target}` (so the "
            f"new task runs after the old one)\n"
            f"  - include `migration` in its `tags` list (the orchestrator uses "
            f"this to branch the sandbox from {update_target}'s output instead "
            f"of base, so the worker sees and edits the old code)\n"
            f"  - target the same files {update_target} produced where it makes "
            f"sense, and describe the change as a delta on top of the old "
            f"implementation (think database migration, not full rewrite)\n"
        )

    parts.append(
        "## Feature scope\n\n"
        f"{feature.strip()}\n\n"
        "Decompose this into a `tasks.md` per the skill instructions. "
        "If a project root is available, inspect it (Read/Grep/Glob) to align "
        "file paths and conventions with the existing codebase before emitting."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def generate_tasks_md(
    config: AppConfig,
    *,
    feature: str,
    srs_path: Path | None = None,
    project_root: Path | None = None,
    existing_specs: list[TaskSpec] | None = None,
    starting_id: str | None = None,
    update_target: str | None = None,
) -> str:
    """Generate the contents of tasks.md.

    Append-mode parameters:
      - ``existing_specs``: tasks already in the target file. Surfaced in the
        prompt so the model knows what is already covered.
      - ``starting_id``: first id the model should assign to its new tasks.
      - ``update_target``: when set, instructs the model to emit migration-
        style tasks that depend on this id and tag themselves ``migration``.
    """
    pool = build_claude_pool(config)
    if pool is None:
        raise RuntimeError(
            "Claude pool is disabled in config. Enable claude.enabled to use generate-tasks."
        )

    system_prompt = load_skill_prompt()
    srs_text = read_srs(srs_path) if srs_path else None
    srs_name = srs_path.name if srs_path else None
    user_prompt = build_user_prompt(
        feature,
        srs_text,
        srs_name,
        existing_specs=existing_specs,
        starting_id=starting_id,
        update_target=update_target,
    )

    cwd = project_root or config.project_root_path()

    async def _call():
        async with pool.acquire() as client:
            return await client.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                cwd=cwd,
                allowed_tools=["Read", "Grep", "Glob"],  # read-only
                model=config.claude.task_generator_model,
            )

    result = await retry_async(_call, config=config.retry, op_name="generate_tasks")
    cleaned = _strip_wrapper_fences(result.text)
    return _dedupe_task_sections(cleaned)


def _strip_wrapper_fences(text: str) -> str:
    """If the model wrapped the whole thing in ```markdown ... ```, strip it.

    Inner code blocks (which are valid markdown) are left alone.
    """
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip() + "\n"
    return text + ("\n" if not text.endswith("\n") else "")


# Matches `# Task: T001 — Title` (any of `:`, `-`, `—`, `–` between id and title).
# Captures the id only.  Indentation is allowed because some models emit a
# leading space on header lines.
_TASK_HEADER_RE = re.compile(r"^\s*#\s+Task[:\s\-—–]+([A-Za-z0-9_.-]+)\b")


def _dedupe_task_sections(text: str) -> str:
    """Drop duplicate `# Task: <id> ...` sections, keeping the first.

    Two failure modes this guards against:

    1. The model repeats its entire output (sometimes after a tool call,
       sometimes under a long-context drift). Without this, the orchestrator
       collapses N duplicates into 1 silently and the user is mystified why
       their 34-section file became a 17-task run.
    2. The model emits a stray summary section with a recycled id at the end.

    A duplicate is detected purely by id equality. We do not try to verify
    the duplicate's body matches — different bodies for the same id are
    still wrong, and keeping the first is at least deterministic.
    """
    lines = text.splitlines()

    # Locate every task header and its id.
    headers: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _TASK_HEADER_RE.match(line)
        if m:
            headers.append((i, m.group(1)))

    if not headers:
        return text  # No task headers — nothing to dedupe.

    seen: set[str] = set()
    dropped: list[str] = []
    kept_lines: list[str] = []

    # Pre-amble (anything before the first header) is kept verbatim.
    if headers[0][0] > 0:
        kept_lines.extend(lines[: headers[0][0]])

    for idx, (start, tid) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        if tid in seen:
            dropped.append(tid)
            continue
        seen.add(tid)
        kept_lines.extend(lines[start:end])

    if dropped:
        log.warning(
            "generate-tasks: dropped %d duplicate task section(s); kept first occurrence of each. Repeated ids: %s",
            len(dropped),
            sorted(set(dropped)),
        )

    out = "\n".join(kept_lines)
    return out + ("\n" if out and not out.endswith("\n") else "")
