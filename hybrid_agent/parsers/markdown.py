"""Markdown parser.

Section format:

    # Task: T001 - Create student API
    - depends_on: T000
    - tags: api, crud
    - complexity: low
    - files: src/api/student.py
    - acceptance: POST /students returns 201

    Description body here. Multi-line OK.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import TaskSpec
from .base import build_spec, register

_FIELD_RE = re.compile(r"^[-*]\s*([A-Za-z_][A-Za-z0-9_ -]*)\s*:\s*(.*)$")
_HEADER_ID_TITLE_RE = re.compile(r"^(?:Task[:\s\-—–]+)?([A-Za-z0-9_.-]+)\s*[-:—–]\s*(.+)$")


def parse_markdown_text(text: str) -> list[TaskSpec]:
    """Parse markdown text into a list of TaskSpec.

    Splits on top-level (`# `) headers; each section becomes one task.
    Exposed so the Word parser can reuse it.
    """
    sections: list[list[str]] = []
    current: list[str] | None = None

    for line in text.splitlines():
        if line.lstrip().startswith("# "):
            if current is not None:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current:
        sections.append(current)

    out: list[TaskSpec] = []
    for sec in sections:
        spec = _parse_section(sec)
        if spec is not None:
            out.append(spec)
    return out


def _parse_section(lines: list[str]) -> TaskSpec | None:
    if not lines:
        return None
    header = lines[0].lstrip("# ").strip()

    m = _HEADER_ID_TITLE_RE.match(header)
    if m:
        tid, title = m.group(1).strip(), m.group(2).strip()
    else:
        tid, title = header.strip(), header.strip()

    fields: dict[str, Any] = {"id": tid, "title": title}
    desc_lines: list[str] = []
    in_fields = True

    for line in lines[1:]:
        if in_fields:
            fm = _FIELD_RE.match(line)
            if fm:
                key = fm.group(1).strip().lower().replace(" ", "_").replace("-", "_")
                fields[key] = fm.group(2).strip()
                continue
            if line.strip() == "":
                in_fields = False
                continue
            in_fields = False
            desc_lines.append(line)
        else:
            desc_lines.append(line)

    fields["description"] = "\n".join(desc_lines).strip()
    return build_spec(fields)


class MarkdownParser:
    extensions: tuple[str, ...] = (".md", ".markdown")

    def parse(self, path: Path) -> list[TaskSpec]:
        with open(path, encoding="utf-8") as f:
            return parse_markdown_text(f.read())


register(MarkdownParser())
