"""Project language detection from marker files.

Used by the reviewer to pick a sensible default test command when the user
hasn't set `orchestrator.test_command` explicitly.

The detection is a flat priority list — the first marker file found wins.
That matters for polyglot projects: a Python+JS project with `pyproject.toml`
above `package.json` in the list will be classified as Python. Users with
mixed projects should set `test_command` explicitly in config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LangProfile:
    name: str
    test_command: str
    description: str


# Order matters — first match wins. Backend / test-heavy languages first since
# those are most likely the "primary" lang in a mixed repo.
_DETECTORS: list[tuple[tuple[str, ...], LangProfile]] = [
    (
        ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
        LangProfile("python", "pytest -x --tb=short", "Python (pytest)"),
    ),
    (
        ("go.mod",),
        LangProfile("go", "go test ./...", "Go"),
    ),
    (
        ("Cargo.toml",),
        LangProfile("rust", "cargo test --quiet", "Rust"),
    ),
    (
        ("pom.xml",),
        LangProfile("java-maven", "mvn -q test", "Java (Maven)"),
    ),
    (
        ("build.gradle", "build.gradle.kts", "settings.gradle"),
        LangProfile("java-gradle", "gradle test --quiet", "Java/Kotlin (Gradle)"),
    ),
    (
        ("Gemfile",),
        LangProfile("ruby", "bundle exec rspec", "Ruby (RSpec)"),
    ),
    (
        ("composer.json",),
        LangProfile("php", "vendor/bin/phpunit", "PHP (PHPUnit)"),
    ),
    (
        ("mix.exs",),
        LangProfile("elixir", "mix test", "Elixir"),
    ),
    # Node last — most repos that *also* have a Python backend will hit Python first.
    (
        ("package.json",),
        LangProfile("node", "npm test --silent", "Node.js (npm test)"),
    ),
]


def detect_language(project_dir: Path) -> LangProfile | None:
    """Return the first matching language profile, or None if nothing detected.

    Looks only at the project root — not subdirectories — to avoid false
    positives from `node_modules/.../package.json` etc.
    """
    for markers, profile in _DETECTORS:
        for marker in markers:
            if (project_dir / marker).is_file():
                return profile
    return None
