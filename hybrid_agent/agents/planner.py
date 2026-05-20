"""Planner agent. Always uses Claude (frontier intelligence is most valuable here).

Given a TaskSpec and the project root, the planner inspects relevant files
and emits a structured PlanOutput the worker can follow.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..config import AppConfig
from ..llm import LLMPool
from ..models import PlanOutput, TaskSpec

log = logging.getLogger(__name__)


PLANNER_SYSTEM_PROMPT = """You are a senior tech lead breaking down a coding task for a junior developer.

Your output must be a SINGLE JSON object (no prose, no markdown fences) with these keys:
{
  "files_to_modify": ["path/to/existing/file.py"],
  "files_to_create": ["path/to/new/file.py"],
  "approach": "Concrete step-by-step instructions. Reference real symbols.",
  "test_strategy": "What to test and how.",
  "estimated_loc": 120,
  "context_snippets": {
    "path/to/file.py": "Relevant excerpt the implementer must follow"
  }
}

Hard requirements:
- Inspect the codebase first (Read/Grep/Glob) before planning.
- Match existing conventions: naming, error handling, layering, validation.
- The implementer is a smaller model. Be EXPLICIT, not vague.
- For each file_to_modify, include a context_snippet showing the relevant existing code.
- If the task is ambiguous, make reasonable assumptions and state them in `approach`.
- Keep estimated_loc realistic.
"""


def _build_user_prompt(task: TaskSpec) -> str:
    parts = [
        f"# Task: {task.id} — {task.title}",
        "",
        f"Description: {task.description or '(none)'}",
        f"Tags: {', '.join(task.tags) or '(none)'}",
        f"Complexity hint: {task.complexity.value}",
        f"Target files (from spec): {', '.join(task.target_files) or '(unspecified)'}",
        f"Acceptance criteria: {task.acceptance_criteria or '(none)'}",
        "",
        "Inspect the codebase to confirm the right files, conventions, and "
        "existing patterns. Then emit the JSON plan.",
    ]
    return "\n".join(parts)


def _strip_fences(text: str) -> str:
    """Models sometimes wrap JSON in ```json ... ``` despite instructions."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_json(text: str) -> dict:
    """Extract the first JSON object from text robustly."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find {...} balanced span
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object found in planner output: {text[:200]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"unterminated JSON in planner output: {text[:200]}")


class Planner:
    def __init__(self, claude_pool: LLMPool, config: AppConfig) -> None:
        self.pool = claude_pool
        self.config = config

    async def plan(self, task: TaskSpec, project_root: Path) -> PlanOutput:
        async with self.pool.acquire() as client:
            result = await client.complete(
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_prompt=_build_user_prompt(task),
                cwd=project_root,
                allowed_tools=["Read", "Grep", "Glob"],  # planner is read-only
                model=self.config.claude.planner_model,
            )

        try:
            data = _extract_json(result.text)
        except Exception as exc:
            log.error("Planner JSON parse failed: %s\n---\n%s", exc, result.text[:1000])
            # Degrade: treat the whole text as the approach
            return PlanOutput(approach=result.text, raw_text=result.text)

        return PlanOutput(
            files_to_modify=list(data.get("files_to_modify") or []),
            files_to_create=list(data.get("files_to_create") or []),
            approach=str(data.get("approach") or ""),
            test_strategy=str(data.get("test_strategy") or ""),
            estimated_loc=int(data.get("estimated_loc") or 0),
            context_snippets=dict(data.get("context_snippets") or {}),
            raw_text=result.text,
        )
