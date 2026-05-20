"""Word (.docx) parser.

Strategy: convert paragraphs to markdown-style text (Heading 1 -> `# ...`),
then delegate to the markdown parser. This keeps a single source of truth
for the section schema.
"""

from __future__ import annotations

from pathlib import Path

from ..models import TaskSpec
from .base import register
from .markdown import parse_markdown_text


class WordParser:
    extensions: tuple[str, ...] = (".docx",)

    def parse(self, path: Path) -> list[TaskSpec]:
        from docx import Document  # lazy import

        doc = Document(str(path))

        lines: list[str] = []
        for p in doc.paragraphs:
            txt = p.text.rstrip()
            style = (p.style.name if p.style else "") or ""
            if style.startswith("Heading"):
                lines.append("# " + txt)
            else:
                lines.append(txt)
        return parse_markdown_text("\n".join(lines))


register(WordParser())
