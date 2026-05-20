"""Tests for `hybrid_agent.language.detect_language`."""
from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_agent.language import detect_language


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.mark.parametrize(
    "marker,expected_name",
    [
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("requirements.txt", "python"),
        ("package.json", "node"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("pom.xml", "java-maven"),
        ("build.gradle", "java-gradle"),
        ("build.gradle.kts", "java-gradle"),
        ("Gemfile", "ruby"),
        ("composer.json", "php"),
        ("mix.exs", "elixir"),
    ],
)
def test_single_marker(tmp_path: Path, marker: str, expected_name: str):
    _touch(tmp_path / marker, "stub")
    profile = detect_language(tmp_path)
    assert profile is not None
    assert profile.name == expected_name
    assert profile.test_command  # non-empty


def test_python_takes_priority_over_node(tmp_path: Path):
    _touch(tmp_path / "pyproject.toml", "stub")
    _touch(tmp_path / "package.json", "{}")
    assert detect_language(tmp_path).name == "python"


def test_empty_dir_returns_none(tmp_path: Path):
    assert detect_language(tmp_path) is None


def test_subdirectory_marker_not_picked_up(tmp_path: Path):
    """A package.json deep in node_modules shouldn't classify the project."""
    _touch(tmp_path / "node_modules" / "foo" / "package.json", "{}")
    assert detect_language(tmp_path) is None


def test_directory_with_marker_name_is_not_match(tmp_path: Path):
    # If something happens to be a directory named pyproject.toml, skip it
    (tmp_path / "pyproject.toml").mkdir()
    assert detect_language(tmp_path) is None
