"""Terminal UI for hybrid_agent (Textual-based).

Lazy entry point so users without `textual` installed can still use the rest
of the package. Call `run_tui(config_path, tasks_path)` — it imports textual
on demand and raises a clear error if the optional dependency is missing.
"""

from __future__ import annotations

from pathlib import Path


def run_tui(config_path: Path, tasks_path: Path) -> None:
    try:
        from .app import HybridAgentApp
    except ImportError as exc:
        raise SystemExit(
            "TUI dependencies missing. Install with:\n"
            "    pip install -e '.[tui]'\n"
            f"(original error: {exc})"
        ) from exc
    HybridAgentApp(config_path=config_path, tasks_path=tasks_path).run()
