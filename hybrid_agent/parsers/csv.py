"""CSV parser.

Note: this module is named `csv.py` but `import csv` resolves to the stdlib
because absolute imports are the default in Python 3.
"""

from __future__ import annotations

import csv as _stdlib_csv
from pathlib import Path

from ..models import TaskSpec
from .base import build_spec, register


class CsvParser:
    extensions: tuple[str, ...] = (".csv",)

    def parse(self, path: Path) -> list[TaskSpec]:
        with open(path, encoding="utf-8", newline="") as f:
            reader = _stdlib_csv.DictReader(f)
            return [build_spec(row) for row in reader if (row.get("id") or "").strip()]


register(CsvParser())
