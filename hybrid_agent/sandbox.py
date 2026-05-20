"""Per-task sandbox using git worktrees.

Each task gets its own worktree at <base_dir>/<task_id> on a branch
<branch_prefix><task_id>. Isolation lets workers run in parallel without
stepping on each other.

Falls back to a plain copy if the project isn't a git repo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from .config import SandboxConfig

log = logging.getLogger(__name__)


class SandboxError(Exception):
    pass


def _safe_branch(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_./-]+", "-", name).strip("-")


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class SandboxManager:
    def __init__(self, project_root: Path, config: SandboxConfig) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.base_dir = (self.project_root / config.base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._is_git: bool | None = None

    async def _check_git(self) -> bool:
        if self._is_git is not None:
            return self._is_git
        rc, _, _ = await _run(["git", "rev-parse", "--git-dir"], cwd=self.project_root)
        self._is_git = rc == 0
        if not self._is_git:
            log.warning("Project root is not a git repo; falling back to copy-based sandbox")
        return self._is_git

    async def create(
        self,
        task_id: str,
        *,
        from_branch: str | None = None,
    ) -> tuple[Path, str]:
        """Create sandbox; returns (path, branch_name).

        ``from_branch`` lets the caller seed the new worktree from a branch
        other than ``config.base_branch`` — used by migration tasks so the
        worker sees the upstream task's code instead of bare base.
        """
        sandbox_path = self.base_dir / _safe_branch(task_id)
        branch = _safe_branch(self.config.branch_prefix + task_id)

        # Idempotent: if sandbox already exists (resume), reuse it.
        if sandbox_path.exists():
            log.info("Reusing existing sandbox: %s", sandbox_path)
            return sandbox_path, branch

        if self.config.use_git_worktree and await self._check_git():
            await self._create_worktree(sandbox_path, branch, from_branch=from_branch)
        else:
            await self._create_copy(sandbox_path)

        return sandbox_path, branch

    async def _create_worktree(
        self,
        path: Path,
        branch: str,
        *,
        from_branch: str | None = None,
    ) -> None:
        # Resolve the branch to fork from. Caller-supplied takes priority
        # (migration mode), otherwise fall back to configured base, otherwise
        # current HEAD if the configured base doesn't exist yet.
        candidates: list[str] = []
        if from_branch:
            candidates.append(from_branch)
        candidates.append(self.config.base_branch)

        base = "HEAD"
        for cand in candidates:
            rc, _, _ = await _run(
                ["git", "rev-parse", "--verify", cand], cwd=self.project_root
            )
            if rc == 0:
                base = cand
                break

        if from_branch and base != from_branch:
            log.warning(
                "Requested from_branch %r not found; falling back to %s",
                from_branch,
                base,
            )

        # Tear down anything a previous run may have left at this slot:
        #   1. a registered worktree at `path`  → blocks `worktree add`
        #   2. a dangling worktree entry         → blocks even after rmdir
        #   3. the branch itself                 → blocks `worktree add -b`
        # All three commands are no-ops when the target doesn't exist; we
        # don't check rc because the source of truth is whether the
        # subsequent `worktree add` succeeds.
        await _run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=self.project_root,
        )
        await _run(["git", "worktree", "prune"], cwd=self.project_root)
        await _run(["git", "branch", "-D", branch], cwd=self.project_root)

        rc, out, err = await _run(
            ["git", "worktree", "add", "-b", branch, str(path), base],
            cwd=self.project_root,
        )
        if rc != 0:
            raise SandboxError(f"git worktree add failed: {err or out}")
        log.info("Created worktree %s on branch %s (forked from %s)", path, branch, base)

    async def _create_copy(self, path: Path) -> None:
        ignore = shutil.ignore_patterns(
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            self.config.base_dir,
        )
        # shutil.copytree is sync, run in executor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: shutil.copytree(self.project_root, path, ignore=ignore),
        )

    async def commit_all(self, sandbox_path: Path, message: str) -> str | None:
        """Stage and commit everything in the sandbox. Returns commit SHA."""
        if not await self._check_git():
            return None
        rc, _, _ = await _run(["git", "add", "-A"], cwd=sandbox_path)
        if rc != 0:
            return None
        # Skip if no changes
        rc, out, _ = await _run(["git", "status", "--porcelain"], cwd=sandbox_path)
        if not out.strip():
            log.info("No changes to commit in %s", sandbox_path)
            return None
        rc, _, err = await _run(
            [
                "git",
                "-c",
                "user.name=hybrid-agent",
                "-c",
                "user.email=agent@local",
                "commit",
                "-m",
                message,
            ],
            cwd=sandbox_path,
        )
        if rc != 0:
            log.warning("commit failed: %s", err)
            return None
        rc, sha, _ = await _run(["git", "rev-parse", "HEAD"], cwd=sandbox_path)
        return sha.strip() if rc == 0 else None

    async def diff(self, sandbox_path: Path) -> str:
        """Diff vs base branch."""
        if not await self._check_git():
            return ""
        rc, out, _ = await _run(
            ["git", "diff", self.config.base_branch + "...HEAD"],
            cwd=sandbox_path,
        )
        return out if rc == 0 else ""

    async def cleanup(self, sandbox_path: Path, branch: str) -> None:
        if self.config.use_git_worktree and await self._check_git():
            await _run(
                ["git", "worktree", "remove", "--force", str(sandbox_path)], cwd=self.project_root
            )
            await _run(["git", "branch", "-D", branch], cwd=self.project_root)
        else:
            if sandbox_path.exists():
                shutil.rmtree(sandbox_path, ignore_errors=True)
