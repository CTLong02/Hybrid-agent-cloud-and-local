"""Read/write tasks.md in the format the existing markdown parser expects.

Reading delegates to `hybrid_agent.parsers.markdown.parse_markdown_text`.
Writing reconstructs the section format used in tasks.example.md so a
round-trip through the TUI keeps files reviewable.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Complexity, TaskSpec
from ..parsers.markdown import parse_markdown_text


def load_tasks(path: Path) -> list[TaskSpec]:
    if not path.exists():
        return []
    return parse_markdown_text(path.read_text(encoding="utf-8"))


def dump_tasks(path: Path, specs: list[TaskSpec]) -> None:
    path.write_text(serialize_tasks(specs), encoding="utf-8")


def serialize_tasks(specs: list[TaskSpec]) -> str:
    return "\n\n".join(_serialize_one(s) for s in specs) + "\n"


def _serialize_one(s: TaskSpec) -> str:
    lines = [f"# Task: {s.id} — {s.title}"]
    lines.append(f"- depends_on: {', '.join(s.depends_on)}")
    lines.append(f"- tags: {', '.join(s.tags)}")
    lines.append(f"- complexity: {s.complexity.value}")
    lines.append(f"- files: {', '.join(s.target_files)}")
    ac = s.acceptance_criteria or ""
    if "\n" in ac:
        body = "\n    ".join(ac.splitlines())
        lines.append(f"- acceptance: |\n    {body}")
    else:
        lines.append(f"- acceptance: {ac}")
    if s.description:
        lines.append("")
        lines.append(s.description.rstrip())
    return "\n".join(lines)


def make_blank(task_id: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title="(new task)",
        complexity=Complexity.MEDIUM,
    )


def next_task_id(specs: list[TaskSpec], prefix: str = "T") -> str:
    """Return the next T### id that doesn't collide with existing ones."""
    nums: list[int] = []
    for s in specs:
        if not s.id.startswith(prefix):
            continue
        try:
            nums.append(int(s.id[len(prefix):]))
        except ValueError:
            continue
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n:03d}"
