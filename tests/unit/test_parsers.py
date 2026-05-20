"""Tests for `hybrid_agent.parsers` (markdown / yaml / json / csv + base helpers).

Excel and Word parsers are skipped — they're in `coverage.run.omit` because
they require the Office libs to be installed and aren't on the hot path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_agent.models import Complexity
from hybrid_agent.parsers import parse_file, supported_extensions
from hybrid_agent.parsers.base import (
    build_spec,
    split_list,
    to_complexity,
)
from hybrid_agent.parsers.markdown import parse_markdown_text


# ---------------------------------------------------------------------------
# Base helpers (pure)
# ---------------------------------------------------------------------------

class TestSplitList:
    def test_none_or_empty(self):
        assert split_list(None) == []
        assert split_list("") == []

    def test_real_list(self):
        assert split_list(["a", " b ", ""]) == ["a", "b"]

    def test_comma_separated(self):
        assert split_list("a, b, c") == ["a", "b", "c"]

    def test_semicolon_separated(self):
        assert split_list("a; b; c") == ["a", "b", "c"]

    def test_mixed_separators(self):
        assert split_list("a, b; c") == ["a", "b", "c"]


class TestToComplexity:
    def test_already_enum(self):
        assert to_complexity(Complexity.HIGH) == Complexity.HIGH

    def test_lowercase_string(self):
        assert to_complexity("high") == Complexity.HIGH

    def test_invalid_falls_back_to_medium(self):
        assert to_complexity("ultra") == Complexity.MEDIUM

    def test_empty_falls_back_to_medium(self):
        assert to_complexity("") == Complexity.MEDIUM
        assert to_complexity(None) == Complexity.MEDIUM


class TestBuildSpec:
    def test_minimal(self):
        s = build_spec({"id": "T1"})
        assert s.id == "T1"
        assert s.title == "T1"  # falls back to id
        assert s.depends_on == []

    def test_alias_fields(self):
        s = build_spec({
            "id": "T1",
            "title": "Make it work",
            "deps": "T0",                 # alias for depends_on
            "files": "a.py, b.py",         # alias for target_files
            "acceptance": "passes",        # alias for acceptance_criteria
        })
        assert s.depends_on == ["T0"]
        assert s.target_files == ["a.py", "b.py"]
        assert s.acceptance_criteria == "passes"

    def test_unknown_fields_go_to_metadata(self):
        s = build_spec({"id": "T1", "owner": "alice", "priority": "P1"})
        assert s.metadata == {"owner": "alice", "priority": "P1"}


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

MD_SIMPLE = """\
# Task: T001 - First task
- depends_on:
- tags: api, crud
- complexity: low
- files: src/api/foo.py
- acceptance: returns 200

Description spanning
multiple lines.

# Task: T002 - Second task
- deps: T001
- complexity: high

Body.
"""


class TestMarkdown:
    def test_parses_two_tasks(self):
        specs = parse_markdown_text(MD_SIMPLE)
        assert len(specs) == 2
        assert specs[0].id == "T001"
        assert specs[1].id == "T002"

    def test_first_task_fields(self):
        s = parse_markdown_text(MD_SIMPLE)[0]
        assert s.title == "First task"
        assert s.tags == ["api", "crud"]
        assert s.complexity == Complexity.LOW
        assert s.target_files == ["src/api/foo.py"]
        assert s.acceptance_criteria == "returns 200"
        assert "Description" in s.description

    def test_second_task_uses_deps_alias(self):
        s = parse_markdown_text(MD_SIMPLE)[1]
        assert s.depends_on == ["T001"]
        assert s.complexity == Complexity.HIGH

    def test_empty_text(self):
        assert parse_markdown_text("") == []

    def test_loads_via_parse_file(self, tmp_path: Path):
        p = tmp_path / "tasks.md"
        p.write_text(MD_SIMPLE, encoding="utf-8")
        specs = parse_file(p)
        assert len(specs) == 2


# ---------------------------------------------------------------------------
# YAML / JSON parsers
# ---------------------------------------------------------------------------

class TestYamlJson:
    def test_yaml_top_level_list(self, tmp_path: Path):
        p = tmp_path / "tasks.yaml"
        p.write_text(
            "- id: T1\n  title: Foo\n  complexity: high\n"
            "- id: T2\n  title: Bar\n  deps: T1\n"
        )
        specs = parse_file(p)
        assert [s.id for s in specs] == ["T1", "T2"]
        assert specs[0].complexity == Complexity.HIGH
        assert specs[1].depends_on == ["T1"]

    def test_yaml_with_tasks_wrapper(self, tmp_path: Path):
        p = tmp_path / "tasks.yaml"
        p.write_text("tasks:\n  - id: T1\n    title: Foo\n")
        specs = parse_file(p)
        assert specs[0].id == "T1"

    def test_json_top_level_list(self, tmp_path: Path):
        p = tmp_path / "tasks.json"
        p.write_text('[{"id":"T1","title":"Foo"}]')
        specs = parse_file(p)
        assert specs[0].id == "T1"

    def test_json_object_at_top_raises(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text('{"not_tasks": []}')
        with pytest.raises(ValueError, match="expected list"):
            parse_file(p)


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

class TestCsv:
    def test_basic(self, tmp_path: Path):
        p = tmp_path / "tasks.csv"
        p.write_text(
            "id,title,deps,tags,complexity\n"
            "T1,First,,api,low\n"
            "T2,Second,T1,db,medium\n",
            encoding="utf-8",
        )
        specs = parse_file(p)
        assert [s.id for s in specs] == ["T1", "T2"]
        assert specs[1].depends_on == ["T1"]

    def test_skips_blank_id_rows(self, tmp_path: Path):
        p = tmp_path / "tasks.csv"
        p.write_text("id,title\nT1,A\n,B\nT3,C\n", encoding="utf-8")
        specs = parse_file(p)
        assert [s.id for s in specs] == ["T1", "T3"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_supported_extensions_includes_core(self):
        ext = supported_extensions()
        assert ".md" in ext
        assert ".yaml" in ext
        assert ".json" in ext
        assert ".csv" in ext

    def test_unknown_extension_raises(self, tmp_path: Path):
        p = tmp_path / "tasks.unknown"
        p.write_text("")
        with pytest.raises(ValueError, match="No parser"):
            parse_file(p)
