"""Tests for `hybrid_agent.sandbox.SandboxManager`.

Subprocess calls are stubbed with monkeypatch on `_run`. Copy mode runs
against the real filesystem in tmp_path because that's the path the
hybrid_agent project actually uses (workspace isn't a git repo).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from hybrid_agent.config import SandboxConfig
from hybrid_agent.sandbox import SandboxError, SandboxManager, _safe_branch


class TestSafeBranch:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("agent/T001", "agent/T001"),
            ("agent T001", "agent-T001"),
            ("a@b#c", "a-b-c"),
            ("---x---", "x"),
            ("foo.bar_baz", "foo.bar_baz"),
        ],
    )
    def test_sanitize(self, raw, expected):
        assert _safe_branch(raw) == expected


class TestCopyMode:
    """Project root isn't a git repo → SandboxManager falls back to copytree.
    We exercise that real path in tmp."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        root = tmp_path / "project"
        root.mkdir()
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('hi')\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "junk.txt").write_text("ignore me")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_text("bytecode")
        return root

    def _mgr(self, project: Path) -> SandboxManager:
        return SandboxManager(
            project,
            SandboxConfig(
                base_dir=".sandboxes",
                use_git_worktree=False,  # force copy mode
            ),
        )

    async def test_create_copies_project_files(self, project):
        mgr = self._mgr(project)
        sandbox, branch = await mgr.create("T001")
        assert sandbox.is_dir()
        assert (sandbox / "src" / "main.py").read_text() == "print('hi')\n"
        assert branch == "agent/T001"

    async def test_create_excludes_ignore_patterns(self, project):
        mgr = self._mgr(project)
        sandbox, _ = await mgr.create("T001")
        assert not (sandbox / "node_modules").exists()
        assert not (sandbox / "__pycache__").exists()

    async def test_create_is_idempotent_on_resume(self, project):
        mgr = self._mgr(project)
        sandbox1, _ = await mgr.create("T001")
        # Drop a marker; if create() re-copied, it would be wiped
        (sandbox1 / "marker.txt").write_text("ok")
        sandbox2, _ = await mgr.create("T001")
        assert sandbox1 == sandbox2
        assert (sandbox2 / "marker.txt").read_text() == "ok"

    async def test_commit_all_noop_in_copy_mode(self, project):
        mgr = self._mgr(project)
        sandbox, _ = await mgr.create("T001")
        # No git → commit_all returns None gracefully
        sha = await mgr.commit_all(sandbox, "msg")
        assert sha is None

    async def test_diff_empty_in_copy_mode(self, project):
        mgr = self._mgr(project)
        sandbox, _ = await mgr.create("T001")
        diff = await mgr.diff(sandbox)
        assert diff == ""

    async def test_cleanup_removes_directory(self, project):
        mgr = self._mgr(project)
        sandbox, branch = await mgr.create("T001")
        assert sandbox.exists()
        await mgr.cleanup(sandbox, branch)
        assert not sandbox.exists()


class TestGitWorktreeMode:
    """Mock _run so we don't depend on git being installed in CI."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        root = tmp_path / "git_project"
        root.mkdir()
        return root

    @pytest.fixture
    def mgr(self, project: Path) -> SandboxManager:
        return SandboxManager(
            project,
            SandboxConfig(use_git_worktree=True, base_branch="main"),
        )

    async def test_check_git_caches_result(self, mgr, monkeypatch):
        calls = []

        async def fake_run(cmd, cwd=None):
            calls.append(cmd)
            return (0, ".git", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        assert await mgr._check_git() is True  # noqa: SLF001
        assert await mgr._check_git() is True  # noqa: SLF001
        # Cached: only one git rev-parse call
        assert len(calls) == 1

    async def test_check_git_falls_back_when_not_repo(self, mgr, monkeypatch):
        async def fake_run(cmd, cwd=None):
            return (128, "", "fatal: not a git repository")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        assert await mgr._check_git() is False  # noqa: SLF001

    async def test_create_worktree_calls_git(self, mgr, monkeypatch):
        commands: list[list[str]] = []

        async def fake_run(cmd, cwd=None):
            commands.append(cmd)
            if cmd[:2] == ["git", "rev-parse"] and "--git-dir" in cmd:
                return (0, ".git", "")
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return (0, "abcdef", "")
            return (0, "", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        sandbox, branch = await mgr.create("T001")
        # Ensure `git worktree add` was called
        assert any("worktree" in c for c in commands), f"got: {commands}"
        assert branch == "agent/T001"

    async def test_worktree_create_failure_raises(self, mgr, monkeypatch):
        async def fake_run(cmd, cwd=None):
            if "rev-parse" in cmd:
                return (0, ".git", "")
            if "worktree" in cmd:
                return (1, "", "fatal: bad ref")
            return (0, "", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        with pytest.raises(SandboxError, match="worktree"):
            await mgr.create("T001")

    async def test_commit_all_skips_when_no_changes(self, mgr, monkeypatch):
        async def fake_run(cmd, cwd=None):
            if "rev-parse" in cmd:
                return (0, ".git", "")
            if cmd[:2] == ["git", "add"]:
                return (0, "", "")
            if "status" in cmd:
                return (0, "", "")  # empty porcelain → no changes
            return (0, "", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        sha = await mgr.commit_all(Path("/tmp/x"), "msg")
        assert sha is None

    async def test_commit_all_returns_sha_on_success(self, mgr, monkeypatch):
        async def fake_run(cmd, cwd=None):
            if "rev-parse" in cmd and "HEAD" in cmd:
                return (0, "deadbeef\n", "")
            if "rev-parse" in cmd:
                return (0, ".git", "")
            if cmd[:2] == ["git", "add"]:
                return (0, "", "")
            if "status" in cmd:
                return (0, "M file.py\n", "")  # has changes
            if "commit" in cmd:
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        sha = await mgr.commit_all(Path("/tmp/x"), "msg")
        assert sha == "deadbeef"

    async def test_diff_returns_output(self, mgr, monkeypatch):
        async def fake_run(cmd, cwd=None):
            if "rev-parse" in cmd:
                return (0, ".git", "")
            if "diff" in cmd:
                return (0, "diff --git ...", "")
            return (0, "", "")

        monkeypatch.setattr("hybrid_agent.sandbox._run", fake_run)
        out = await mgr.diff(Path("/tmp/x"))
        assert "diff --git" in out
