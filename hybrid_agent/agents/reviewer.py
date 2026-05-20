"""Reviewer agent. Claude with full file-edit authority inside the sandbox.

Configured by user request: reviewer can fix code directly. The reviewer
emits a verdict (approved / needs_fix / rejected) and may have already
fixed the issues by the time it returns.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..config import AppConfig
from ..language import detect_language
from ..llm import LLMPool
from ..models import (
    CodeOutput,
    PlanOutput,
    ReviewOutput,
    ReviewVerdict,
    TaskSpec,
)
from ..test_runner import TestStatus, run_tests

log = logging.getLogger(__name__)


REVIEWER_SYSTEM = """You are a senior code reviewer with the authority to fix
issues directly. You operate inside the implementer's sandbox (a git worktree).

Your responsibilities, in order:
1. Read the changed files and verify they meet the task's acceptance criteria.
2. Check for: bugs, missed edge cases, style violations vs codebase conventions,
   security issues, missing tests, broken existing tests.
3. If issues are MINOR (style, naming, small bug, missing test): FIX them
   yourself using Edit/Write, then run any quick check (Bash) you can.
4. If issues are MAJOR (fundamentally wrong approach, missing core feature):
   do NOT auto-fix. Emit verdict `needs_fix` with a clear issue list so the
   coder can re-do it.
5. If the implementation is unsalvageable (e.g. wrong file, irrelevant code):
   emit `rejected`.

Final message MUST be a SINGLE JSON object (no fences):
{
  "verdict": "approved" | "needs_fix" | "rejected",
  "score": 0.0-1.0,
  "issues": ["..."],
  "auto_fixed": true | false,
  "summary": "one line"
}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _extract_last_json(text: str) -> dict:
    text = _strip_fences(text)
    # Find the LAST balanced {...} object (reviewer ends with verdict JSON)
    candidates: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append((start, i + 1))
                start = -1
    for s, e in reversed(candidates):
        try:
            return json.loads(text[s:e])
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no parseable JSON in reviewer output: {text[-300:]}")


class Reviewer:
    def __init__(self, claude_pool: LLMPool, config: AppConfig) -> None:
        self.pool = claude_pool
        self.config = config

    async def review(
        self,
        task: TaskSpec,
        plan: PlanOutput,
        code: CodeOutput,
        sandbox_root: Path,
        diff: str,
    ) -> ReviewOutput:
        prompt = self._build_prompt(task, plan, code, diff)

        async with self.pool.acquire() as client:
            tools = ["Read", "Grep", "Glob", "Bash"]
            if self.config.orchestrator.auto_fix_on_review:
                tools += ["Edit", "Write"]

            result = await client.complete(
                system_prompt=REVIEWER_SYSTEM,
                user_prompt=prompt,
                cwd=sandbox_root,
                allowed_tools=tools,
                model=self.config.claude.reviewer_model,
            )

        try:
            data = _extract_last_json(result.text)
        except Exception as exc:
            log.error("Reviewer JSON parse failed: %s\n---\n%s", exc, result.text[-1500:])
            review = ReviewOutput(
                verdict=ReviewVerdict.NEEDS_FIX,
                issues=[f"reviewer output unparseable: {exc}"],
                raw_text=result.text,
            )
        else:
            try:
                verdict = ReviewVerdict(data.get("verdict", "needs_fix"))
            except ValueError:
                verdict = ReviewVerdict.NEEDS_FIX
            review = ReviewOutput(
                verdict=verdict,
                score=float(data.get("score") or 0.0),
                issues=list(data.get("issues") or []),
                auto_fixed=bool(data.get("auto_fixed", False)),
                raw_text=result.text,
            )

        # Test gate: even if the LLM said APPROVED, failing tests must
        # downgrade to NEEDS_FIX so we don't ship broken code.
        if self.config.orchestrator.run_tests_in_review:
            review = await self._apply_test_gate(review, sandbox_root)

        return review

    async def _apply_test_gate(
        self,
        review: ReviewOutput,
        sandbox_root: Path,
    ) -> ReviewOutput:
        """Run project tests and merge result into the review.

        APPROVED + tests PASSED  -> APPROVED (unchanged)
        APPROVED + tests FAILED  -> NEEDS_FIX, output appended to issues
        APPROVED + NO_TESTS      -> NEEDS_FIX iff config.require_tests else unchanged
        Already NEEDS_FIX/REJECTED -> kept; failing tests just append more issues
        """
        cfg = self.config.orchestrator
        cmd = cfg.test_command
        lang_name = "configured"
        if not cmd:
            profile = detect_language(sandbox_root)
            if profile is None:
                log.info(
                    "Test gate: no language detected in %s; skipping test run",
                    sandbox_root,
                )
                if cfg.require_tests and review.verdict == ReviewVerdict.APPROVED:
                    review.verdict = ReviewVerdict.NEEDS_FIX
                    review.issues = list(review.issues) + [
                        "require_tests=true but no language marker (pyproject.toml / "
                        "package.json / go.mod / …) found. Add tests + project metadata."
                    ]
                return review
            cmd = profile.test_command
            lang_name = profile.name

        result = await run_tests(
            sandbox_root,
            cmd,
            timeout_seconds=cfg.test_timeout_seconds,
        )

        if result.status == TestStatus.PASSED:
            log.info("Test gate: PASSED (%s, %.1fs)", lang_name, result.duration_s)
            return review

        if result.status == TestStatus.NO_TESTS:
            log.info("Test gate: NO_TESTS (%s) — require_tests=%s", lang_name, cfg.require_tests)
            if cfg.require_tests and review.verdict == ReviewVerdict.APPROVED:
                review.verdict = ReviewVerdict.NEEDS_FIX
                review.issues = list(review.issues) + [
                    f"No tests found (lang={lang_name}, command=`{cmd}`). "
                    "Add tests covering the acceptance criteria."
                ]
            return review

        # FAILED / TIMEOUT / ERROR — always blocks DONE.
        log.warning(
            "Test gate: %s (%s, rc=%d, %.1fs)",
            result.status.value,
            lang_name,
            result.rc,
            result.duration_s,
        )
        if review.verdict == ReviewVerdict.APPROVED:
            review.verdict = ReviewVerdict.NEEDS_FIX
        review.issues = list(review.issues) + [
            f"Tests {result.status.value} (lang={lang_name}, rc={result.rc}). "
            f"Command: `{cmd}`\n\n--- output ---\n{result.output}"
        ]
        return review

    def _build_prompt(
        self,
        task: TaskSpec,
        plan: PlanOutput,
        code: CodeOutput,
        diff: str,
    ) -> str:
        parts = [
            f"# Task: {task.id} — {task.title}",
            f"\nAcceptance criteria: {task.acceptance_criteria or '(none)'}",
            "\n## Plan",
            f"\n{plan.approach}",
            "\n## Implementation summary",
            f"\n{code.summary}",
        ]
        if diff:
            parts.append("\n## Diff vs base branch")
            # Cap diff length to keep prompt manageable
            capped = diff if len(diff) <= 30_000 else diff[:30_000] + "\n... (truncated)"
            parts.append(f"\n```diff\n{capped}\n```")
        parts.append("\nReview now. Auto-fix minor issues. Emit the JSON verdict at the end.")
        return "\n".join(parts)
