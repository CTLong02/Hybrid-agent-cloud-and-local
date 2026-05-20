"""Tests for `hybrid_agent.agents.worker` — LocalWorker + ClaudeWorker."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_agent.agents.worker import (
    ClaudeWorker,
    LocalWorker,
    _extract_json_obj,
    _fix_triple_quoted_content,
    _read_existing,
    _strip_fences,
)
from hybrid_agent.models import CodeOutput, PlanOutput


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_strips_json_fence(self):
        assert _strip_fences('```json\n{"a":1}\n```') == '{"a":1}'

    def test_no_fence_unchanged(self):
        assert _strip_fences('{"a":1}') == '{"a":1}'


class TestFixTripleQuotedContent:
    def test_no_triple_quotes_unchanged(self):
        text = '{"content": "regular escaped string"}'
        assert _fix_triple_quoted_content(text) == text

    def test_simple_triple_quoted_content_fixed(self):
        # The exact pattern from the T004 bug
        text = '{"content": """line one\nline two\nline three"""}'
        fixed = _fix_triple_quoted_content(text)
        assert json.loads(fixed) == {"content": "line one\nline two\nline three"}

    def test_python_docstring_inside_content(self):
        # A Python file that itself starts with """...""" — outer rfind
        # ensures we close at the LAST """ not the first
        text = (
            '{"content": """\n"""docstring"""\nfrom x import y\n"""}'
        )
        fixed = _fix_triple_quoted_content(text)
        d = json.loads(fixed)
        assert "docstring" in d["content"]
        assert "from x import y" in d["content"]

    def test_no_closing_triple_left_alone(self):
        text = '{"content": """unterminated'
        # Should not raise; just return original (json.loads will fail later)
        result = _fix_triple_quoted_content(text)
        assert '"""' in result  # untouched


class TestExtractJsonObj:
    def test_normal_json_passes_through(self):
        assert _extract_json_obj('{"a": 1}') == {"a": 1}

    def test_triple_quoted_content_decoded(self):
        text = '{"summary": "ok", "content": """multi\nline"""}'
        d = _extract_json_obj(text)
        assert d["content"] == "multi\nline"

    def test_brace_walk_falls_through_when_text_has_prose(self):
        text = "Here you go:\n{\"a\": 1}\n  More prose."
        assert _extract_json_obj(text) == {"a": 1}


class TestReadExisting:
    def test_reads_only_listed_files(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("aaa")
        (tmp_path / "b.py").write_text("bbb")
        out = _read_existing(tmp_path, ["a.py"])
        assert out == {"a.py": "aaa"}

    def test_skips_missing_files(self, tmp_path: Path):
        out = _read_existing(tmp_path, ["does_not_exist.py"])
        assert out == {}

    def test_caps_total_bytes(self, tmp_path: Path):
        (tmp_path / "big.py").write_text("x" * 1000)
        out = _read_existing(tmp_path, ["big.py"], max_bytes=100)
        assert "truncated" in out["big.py"]


# ---------------------------------------------------------------------------
# LocalWorker
# ---------------------------------------------------------------------------

class TestLocalWorker:
    @pytest.fixture
    def worker(self, base_config, fake_local_pool):
        return LocalWorker(fake_local_pool, base_config)

    @pytest.fixture
    def plan(self) -> PlanOutput:
        return PlanOutput(
            files_to_modify=[],
            files_to_create=["src/foo.py"],
            approach="create foo",
            test_strategy="pytest",
        )

    async def test_creates_files_in_sandbox(self, worker, fake_local_client, plan, make_spec, tmp_path: Path):
        fake_local_client.queue(json.dumps({
            "summary": "Created foo.py",
            "files": [{
                "path": "src/foo.py",
                "operation": "create",
                "content": "def foo():\n    return 1\n",
            }],
        }))
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert isinstance(result, CodeOutput)
        assert result.summary == "Created foo.py"
        assert result.branch == "agent/T1"
        # File was actually written
        target = tmp_path / "src" / "foo.py"
        assert target.read_text() == "def foo():\n    return 1\n"

    async def test_handles_triple_quoted_python_in_content(self, worker, fake_local_client, plan, make_spec, tmp_path):
        # The exact regression we fixed for T004 — Python file with module docstring
        bad_json = (
            '{"summary": "wrote migration", "files": [{"path": "m.py", '
            '"operation": "create", "content": """\n"""Module doc"""\n'
            'from alembic import op\n"""}]}'
        )
        fake_local_client.queue(bad_json)
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        content = (tmp_path / "m.py").read_text()
        assert "Module doc" in content
        assert "from alembic" in content
        assert result.summary == "wrote migration"

    async def test_writes_both_files_and_tests(self, worker, fake_local_client, plan, make_spec, tmp_path):
        fake_local_client.queue(json.dumps({
            "summary": "x",
            "files": [{"path": "a.py", "operation": "create", "content": "A"}],
            "tests": [{"path": "test_a.py", "operation": "create", "content": "T"}],
        }))
        await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert (tmp_path / "a.py").read_text() == "A"
        assert (tmp_path / "test_a.py").read_text() == "T"

    async def test_skips_entries_missing_path_or_content(self, worker, fake_local_client, plan, make_spec, tmp_path):
        fake_local_client.queue(json.dumps({
            "summary": "x",
            "files": [
                {"path": "a.py", "content": "A"},     # no operation → defaults to modify
                {"path": "b.py"},                      # no content → skipped
                {"content": "no path"},                # no path → skipped
            ],
        }))
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert (tmp_path / "a.py").read_text() == "A"
        assert not (tmp_path / "b.py").exists()
        # Only one valid file change
        assert len(result.files_changed) == 1

    async def test_unparseable_json_raises(self, worker, fake_local_client, plan, make_spec, tmp_path):
        fake_local_client.queue("totally not json")
        with pytest.raises(Exception):
            await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")

    async def test_prompt_includes_existing_file_contents(self, worker, fake_local_client, make_spec, tmp_path):
        # Plan says to modify a file that exists in sandbox → it should be in the prompt
        (tmp_path / "existing.py").write_text("OLD CONTENT")
        plan = PlanOutput(files_to_modify=["existing.py"], approach="...")
        fake_local_client.queue(json.dumps({
            "summary": "x", "files": [],
        }))
        await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        prompt = fake_local_client.calls[0]["user"]
        assert "OLD CONTENT" in prompt

    async def test_summary_truncated_to_500_chars(self, worker, fake_local_client, plan, make_spec, tmp_path):
        long_summary = "x" * 1000
        fake_local_client.queue(json.dumps({
            "summary": long_summary, "files": [],
        }))
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert len(result.summary) <= 500


# ---------------------------------------------------------------------------
# ClaudeWorker
# ---------------------------------------------------------------------------

class TestClaudeWorker:
    @pytest.fixture
    def worker(self, base_config, fake_claude_pool):
        return ClaudeWorker(fake_claude_pool, base_config)

    @pytest.fixture
    def plan(self) -> PlanOutput:
        return PlanOutput(approach="do it", test_strategy="x")

    async def test_extracts_summary_line(self, worker, fake_claude_client, plan, make_spec, tmp_path):
        fake_claude_client.queue(
            "I read some files.\nI wrote new code.\nSUMMARY: implemented foo"
        )
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert result.summary == "implemented foo"
        assert result.branch == "agent/T1"

    async def test_default_summary_when_missing(self, worker, fake_claude_client, plan, make_spec, tmp_path):
        fake_claude_client.queue("did some work but forgot the summary line")
        result = await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        assert result.summary == "claude implemented in sandbox"

    async def test_passes_sandbox_as_cwd(self, worker, fake_claude_client, plan, make_spec, tmp_path):
        fake_claude_client.queue("SUMMARY: done")
        await worker.code(make_spec("T1"), plan, tmp_path, "agent/T1")
        call = fake_claude_client.calls[0]
        assert call["cwd"] == tmp_path
        assert "Edit" in call["allowed_tools"]
        assert "Write" in call["allowed_tools"]
        assert "Bash" in call["allowed_tools"]
