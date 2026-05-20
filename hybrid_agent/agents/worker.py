"""Worker agent. Two implementations sharing a CodeOutput contract:

  - LocalWorker (Ollama / Qwen): emits a JSON file-map; orchestrator writes
    files into the sandbox. Avoids tool-calling complexity which a 30B model
    can't reliably handle in a long agent loop.

  - ClaudeWorker: runs Claude Code SDK inside the sandbox cwd with full
    file-edit tools. Used when routing decides Claude.

Both produce a CodeOutput that the orchestrator commits to git.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..config import AppConfig
from ..llm import LLMPool
from ..models import CodeOutput, FileChange, PlanOutput, TaskSpec

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

LOCAL_WORKER_SYSTEM = """You are an expert software engineer. You will receive
a task with a concrete plan and current file contents. Output ONLY a single
JSON object (no markdown fences, no prose) with this exact shape:

{
  "summary": "one-line summary of changes",
  "files": [
    {"path": "relative/path.py", "operation": "create|modify", "content": "FULL FILE CONTENTS"}
  ],
  "tests": [
    {"path": "tests/test_x.py", "operation": "create|modify", "content": "FULL FILE CONTENTS"}
  ]
}

Rules:
- "content" is the FULL final file contents, not a diff.
- Only include files that actually change.
- Match the codebase's existing style and conventions exactly.
- Include tests when the plan calls for them.
- Output JSON only. No explanations before or after.
- CRITICAL: "content" must be a valid JSON string. Use \\n for newlines, \\t for
  tabs, and \\" for double-quotes. NEVER use Python triple-quotes (\\"\\"\\" or
  similar) inside a JSON string — they break JSON parsing.
"""


CLAUDE_WORKER_SYSTEM = """You are an expert software engineer working inside
a sandboxed git worktree. Implement the task per the provided plan.

Rules:
- Match the existing codebase conventions exactly (read neighboring files first).
- Use Edit/Write to modify files in place.
- Run any quick test commands you can to verify (Bash).
- When done, write a one-line summary as your final message starting with `SUMMARY:`.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _fix_triple_quoted_content(text: str) -> str:
    """Convert Python triple-quoted content values to valid JSON strings.

    Local LLMs sometimes emit  "content": \"\"\"...\"\"\"  instead of a proper
    JSON string.  We process content fields from last to first so that nested
    triple-quotes inside a file (e.g. Python docstrings) are handled correctly:
    the closing triple-quote for each field is always the LAST \"\"\" remaining
    after the opening, which rfind() gives us.
    """
    while True:
        matches = list(re.finditer(r'"content"\s*:\s*"""', text))
        if not matches:
            break
        last_m = matches[-1]
        content_start = last_m.end()
        closing = text.rfind('"""', content_start)
        if closing <= content_start - 1:
            break
        content = text[content_start:closing]
        text = text[: last_m.start()] + f'"content": {json.dumps(content)}' + text[closing + 3 :]
    return text


def _extract_json_obj(text: str) -> dict:
    text = _strip_fences(text)
    fixed = _fix_triple_quoted_content(text)
    for candidate in (fixed, text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # Brace-walking fallback on the preprocessed text
    start = fixed.find("{")
    if start < 0:
        raise ValueError(f"no JSON object: {text[:200]}")
    depth = 0
    for i in range(start, len(fixed)):
        if fixed[i] == "{":
            depth += 1
        elif fixed[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(fixed[start : i + 1])
    raise ValueError(f"unterminated JSON: {text[:200]}")


def _read_existing(
    sandbox_root: Path, paths: list[str], max_bytes: int = 200_000
) -> dict[str, str]:
    """Read existing files from the sandbox, capped to keep prompt size sane."""
    out: dict[str, str] = {}
    total = 0
    for rel in paths:
        p = sandbox_root / rel
        if not p.exists() or not p.is_file():
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if total + len(data) > max_bytes:
            data = data[: max(0, max_bytes - total)] + "\n# ...truncated..."
        out[rel] = data
        total += len(data)
        if total >= max_bytes:
            break
    return out


# ---------------------------------------------------------------------------
# Local worker
# ---------------------------------------------------------------------------


class LocalWorker:
    def __init__(self, local_pool: LLMPool, config: AppConfig) -> None:
        self.pool = local_pool
        self.config = config

    def _build_prompt(self, task: TaskSpec, plan: PlanOutput, existing: dict[str, str]) -> str:
        parts = [
            f"# Task: {task.id} — {task.title}",
            f"\nDescription: {task.description or '(none)'}",
            f"\nAcceptance criteria: {task.acceptance_criteria or '(none)'}",
            "\n## Plan from senior tech lead",
            f"\nApproach:\n{plan.approach}",
            f"\nTest strategy:\n{plan.test_strategy}",
            f"\nFiles to modify: {', '.join(plan.files_to_modify) or '(none)'}",
            f"\nFiles to create: {', '.join(plan.files_to_create) or '(none)'}",
        ]
        if plan.context_snippets:
            parts.append("\n## Context snippets from senior")
            for path, snip in plan.context_snippets.items():
                parts.append(f"\n### {path}\n```\n{snip}\n```")
        if existing:
            parts.append("\n## Current file contents (full)")
            for path, content in existing.items():
                parts.append(f"\n### {path}\n```\n{content}\n```")
        parts.append(
            "\nNow output the JSON file-map as specified in the system prompt. Output JSON only."
        )
        return "\n".join(parts)

    async def code(
        self,
        task: TaskSpec,
        plan: PlanOutput,
        sandbox_root: Path,
        branch: str,
    ) -> CodeOutput:
        existing = _read_existing(sandbox_root, plan.files_to_modify)
        prompt = self._build_prompt(task, plan, existing)

        async with self.pool.acquire() as client:
            result = await client.complete(
                system_prompt=LOCAL_WORKER_SYSTEM,
                user_prompt=prompt,
            )

        try:
            data = _extract_json_obj(result.text)
        except Exception as exc:
            log.error("LocalWorker JSON parse failed: %s\n---\n%s", exc, result.text[:1500])
            raise

        changes: list[FileChange] = []
        for entry in (data.get("files") or []) + (data.get("tests") or []):
            path = entry.get("path")
            content = entry.get("content")
            op = entry.get("operation") or "modify"
            if not path or content is None:
                continue
            changes.append(FileChange(path=path, operation=op, content=content))

        # Apply to sandbox
        for ch in changes:
            target = sandbox_root / ch.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(ch.content, encoding="utf-8")

        return CodeOutput(
            branch=branch,
            files_changed=changes,
            summary=str(data.get("summary") or "")[:500],
            raw_text=result.text,
        )


# ---------------------------------------------------------------------------
# Claude worker (operates directly in sandbox via SDK tools)
# ---------------------------------------------------------------------------


class ClaudeWorker:
    def __init__(self, claude_pool: LLMPool, config: AppConfig) -> None:
        self.pool = claude_pool
        self.config = config

    async def code(
        self,
        task: TaskSpec,
        plan: PlanOutput,
        sandbox_root: Path,
        branch: str,
    ) -> CodeOutput:
        prompt = (
            f"# Task: {task.id} — {task.title}\n\n"
            f"Description: {task.description or '(none)'}\n"
            f"Acceptance criteria: {task.acceptance_criteria or '(none)'}\n\n"
            f"## Plan\n\nApproach:\n{plan.approach}\n\n"
            f"Test strategy:\n{plan.test_strategy}\n\n"
            f"Files to modify: {', '.join(plan.files_to_modify) or '(none)'}\n"
            f"Files to create: {', '.join(plan.files_to_create) or '(none)'}\n\n"
            "Implement now. End with `SUMMARY: <one line>`."
        )

        async with self.pool.acquire() as client:
            result = await client.complete(
                system_prompt=CLAUDE_WORKER_SYSTEM,
                user_prompt=prompt,
                cwd=sandbox_root,
                allowed_tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
                model=self.config.claude.coder_model,
            )

        # Pull SUMMARY line out
        summary = ""
        for line in result.text.splitlines():
            if line.strip().startswith("SUMMARY:"):
                summary = line.split(":", 1)[1].strip()
                break

        # Files were modified directly via Edit/Write tools; we don't track them
        # individually here — the orchestrator will diff vs base branch.
        return CodeOutput(
            branch=branch,
            summary=summary or "claude implemented in sandbox",
            raw_text=result.text,
        )
