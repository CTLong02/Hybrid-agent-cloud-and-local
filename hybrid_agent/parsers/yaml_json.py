"""YAML and JSON parser.

Accepts either a top-level list of tasks, or a `{"tasks": [...]}` wrapper.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..models import TaskSpec
from .base import build_spec, register


class YamlJsonParser:
    extensions: tuple[str, ...] = (".yaml", ".yml", ".json")

    def parse(self, path: Path) -> list[TaskSpec]:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)

        if isinstance(data, dict) and "tasks" in data:
            data = data["tasks"]
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected list of tasks at top level")
        return [build_spec(d) for d in data]


register(YamlJsonParser())
