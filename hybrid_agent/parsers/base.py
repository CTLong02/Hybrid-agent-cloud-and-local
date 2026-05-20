"""Task list parser registry + shared helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from ..models import Complexity, TaskSpec


class TaskParser(Protocol):
    # Concrete parsers must annotate their attribute as `tuple[str, ...]`
    # (not the inferred fixed-arity tuple) — Protocol attributes are
    # invariant, so a literal `(".md",)` would otherwise mismatch.
    extensions: tuple[str, ...]

    def parse(self, path: Path) -> list[TaskSpec]: ...


_REGISTRY: dict[str, TaskParser] = {}


def register(parser: TaskParser) -> None:
    for ext in parser.extensions:
        _REGISTRY[ext.lower()] = parser


def parse_file(path: str | Path) -> list[TaskSpec]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in _REGISTRY:
        raise ValueError(f"No parser for extension {ext!r}. Supported: {sorted(_REGISTRY)}")
    return _REGISTRY[ext].parse(p)


def supported_extensions() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Shared helpers used by every concrete parser
# ---------------------------------------------------------------------------


def split_list(value: Any) -> list[str]:
    """Parse a list field that may arrive as a real list, comma/semicolon
    string, or empty value."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    s = str(value)
    parts = re.split(r"[,;]\s*", s)
    return [p.strip() for p in parts if p.strip()]


def to_complexity(value: Any) -> Complexity:
    if isinstance(value, Complexity):
        return value
    if value is None or value == "":
        return Complexity.MEDIUM
    s = str(value).strip().lower()
    try:
        return Complexity(s)
    except ValueError:
        return Complexity.MEDIUM


_KNOWN_FIELDS = {
    "id",
    "title",
    "description",
    "depends_on",
    "deps",
    "tags",
    "complexity",
    "target_files",
    "files",
    "file",
    "acceptance_criteria",
    "acceptance",
}


def build_spec(d: dict[str, Any]) -> TaskSpec:
    """Build a TaskSpec from a normalized dict produced by any parser."""
    return TaskSpec(
        id=str(d["id"]).strip(),
        title=str(d.get("title", d["id"])).strip(),
        description=str(d.get("description", "")).strip(),
        depends_on=split_list(d.get("depends_on") or d.get("deps")),
        tags=split_list(d.get("tags")),
        complexity=to_complexity(d.get("complexity")),
        target_files=split_list(d.get("target_files") or d.get("files") or d.get("file")),
        acceptance_criteria=str(d.get("acceptance_criteria") or d.get("acceptance") or "").strip(),
        metadata={k: v for k, v in d.items() if k not in _KNOWN_FIELDS},
    )
