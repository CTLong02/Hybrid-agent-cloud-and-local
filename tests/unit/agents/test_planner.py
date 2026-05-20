"""Tests for `hybrid_agent.agents.planner.Planner`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_agent.agents.planner import Planner, _extract_json, _strip_fences
from hybrid_agent.models import PlanOutput


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_no_fence_returned_as_is(self):
        assert _strip_fences('{"a": 1}') == '{"a": 1}'

    def test_strips_json_fence(self):
        text = '```json\n{"a": 1}\n```'
        assert _strip_fences(text) == '{"a": 1}'

    def test_strips_bare_fence(self):
        text = '```\n{"a": 1}\n```'
        assert _strip_fences(text) == '{"a": 1}'


class TestExtractJson:
    def test_pure_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_with_prose_prefix(self):
        # Brace-walking should pick up the embedded object
        text = "Here is the plan:\n{\"a\": 1, \"b\": [2, 3]}\nThanks."
        assert _extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="no JSON"):
            _extract_json("just prose, no braces")

    def test_unterminated_json_raises(self):
        with pytest.raises(ValueError):
            _extract_json('{"a": 1')


# ---------------------------------------------------------------------------
# Planner.plan()
# ---------------------------------------------------------------------------

class TestPlannerPlan:
    @pytest.fixture
    def planner(self, base_config, fake_claude_pool):
        return Planner(fake_claude_pool, base_config)

    async def test_happy_path(self, planner, fake_claude_client, make_spec, tmp_path: Path):
        plan_json = json.dumps({
            "files_to_modify": ["src/a.py"],
            "files_to_create": ["src/b.py"],
            "approach": "step 1, step 2",
            "test_strategy": "pytest tests/",
            "estimated_loc": 100,
            "context_snippets": {"src/a.py": "def foo():\n    pass"},
        })
        fake_claude_client.queue(plan_json)

        plan = await planner.plan(make_spec("T1", title="Add B"), tmp_path)
        assert isinstance(plan, PlanOutput)
        assert plan.files_to_modify == ["src/a.py"]
        assert plan.files_to_create == ["src/b.py"]
        assert plan.estimated_loc == 100
        assert "src/a.py" in plan.context_snippets

    async def test_strips_markdown_fences(self, planner, fake_claude_client, make_spec, tmp_path):
        fenced = '```json\n{"files_to_modify": ["x.py"], "approach": "do x"}\n```'
        fake_claude_client.queue(fenced)
        plan = await planner.plan(make_spec("T1"), tmp_path)
        assert plan.files_to_modify == ["x.py"]
        assert plan.approach == "do x"

    async def test_unparseable_json_degrades_gracefully(self, planner, fake_claude_client, make_spec, tmp_path):
        # Returns a PlanOutput with the raw text as approach instead of crashing
        fake_claude_client.queue("I cannot make a plan, the task is unclear.")
        plan = await planner.plan(make_spec("T1"), tmp_path)
        assert plan.approach.startswith("I cannot")
        assert plan.files_to_modify == []
        assert plan.files_to_create == []

    async def test_passes_planner_model_to_client(self, planner, fake_claude_client, make_spec, tmp_path, base_config):
        base_config.claude.planner_model = "claude-sonnet-4-5"
        fake_claude_client.queue('{"approach": "x"}')
        await planner.plan(make_spec("T1"), tmp_path)
        # Verify the client received the model kwarg
        assert fake_claude_client.calls[0]["model"] == "claude-sonnet-4-5"

    async def test_user_prompt_includes_task_metadata(self, planner, fake_claude_client, make_spec, tmp_path):
        fake_claude_client.queue('{"approach": "x"}')
        await planner.plan(
            make_spec("T1", title="Build foo", tags=["api", "auth"], complexity="high",
                      files=["src/foo.py"]),
            tmp_path,
        )
        prompt = fake_claude_client.calls[0]["user"]
        assert "T1" in prompt
        assert "Build foo" in prompt
        assert "api" in prompt
        assert "high" in prompt
        assert "src/foo.py" in prompt

    async def test_planner_uses_readonly_tools(self, planner, fake_claude_client, make_spec, tmp_path):
        fake_claude_client.queue('{"approach": "x"}')
        await planner.plan(make_spec("T1"), tmp_path)
        tools = fake_claude_client.calls[0]["allowed_tools"]
        assert "Read" in tools
        assert "Grep" in tools
        # Planner must NOT have write tools
        assert "Write" not in tools
        assert "Edit" not in tools
        assert "Bash" not in tools
