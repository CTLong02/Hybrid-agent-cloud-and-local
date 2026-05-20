"""Domain models for tasks, executions, and pipeline outputs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    PENDING = "pending"  # In DAG, deps not satisfied
    READY = "ready"  # Deps satisfied, awaiting worker pickup
    PLANNING = "planning"  # Planner running
    PLANNED = "planned"  # Plan saved, awaiting coder
    CODING = "coding"  # Worker coding in sandbox
    CODED = "coded"  # Code saved, awaiting reviewer
    REVIEWING = "reviewing"  # Reviewer running
    FIXING = "fixing"  # Reviewer auto-fixing or worker re-coding
    DONE = "done"  # Successfully completed
    FAILED = "failed"  # Permanently failed
    BLOCKED = "blocked"  # Upstream dep failed

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED}

    @property
    def is_in_progress(self) -> bool:
        return self in {
            TaskStatus.PLANNING,
            TaskStatus.CODING,
            TaskStatus.REVIEWING,
            TaskStatus.FIXING,
        }


class Backend(str, Enum):
    LOCAL = "local"
    CLAUDE = "claude"


class Complexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    NEEDS_FIX = "needs_fix"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Task definitions (input)
# ---------------------------------------------------------------------------


class TaskSpec(BaseModel):
    """A single task as defined by the user (parsed from input file)."""

    id: str
    title: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    complexity: Complexity = Complexity.MEDIUM
    target_files: list[str] = Field(default_factory=list)
    acceptance_criteria: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pipeline outputs
# ---------------------------------------------------------------------------


class PlanOutput(BaseModel):
    """Output of the planner stage."""

    files_to_modify: list[str] = Field(default_factory=list)
    files_to_create: list[str] = Field(default_factory=list)
    approach: str = ""
    test_strategy: str = ""
    estimated_loc: int = 0
    context_snippets: dict[str, str] = Field(default_factory=dict)
    raw_text: str = ""


class FileChange(BaseModel):
    path: str
    operation: str  # "create" | "modify" | "delete"
    content: str = ""


class CodeOutput(BaseModel):
    """Output of the worker (coder) stage."""

    branch: str = ""
    commit_sha: str | None = None
    files_changed: list[FileChange] = Field(default_factory=list)
    summary: str = ""
    tests_passed: bool = False
    test_output: str = ""
    raw_text: str = ""


class ReviewOutput(BaseModel):
    """Output of the reviewer stage."""

    verdict: ReviewVerdict
    score: float = 0.0
    issues: list[str] = Field(default_factory=list)
    auto_fixed: bool = False
    fix_diff: str = ""
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Runtime execution state (persisted)
# ---------------------------------------------------------------------------


class TaskExecution(BaseModel):
    """Per-task runtime state. Persisted to SQLite after every transition."""

    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    backend: Backend | None = None
    routing_reason: str = ""
    attempts: int = 0
    review_iterations: int = 0
    plan: PlanOutput | None = None
    code: CodeOutput | None = None
    review: ReviewOutput | None = None
    last_error: str = ""
    sandbox_path: str = ""
    sandbox_branch: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
