"""Task dependency graph.

Computes which tasks are ready to run given the current execution states.
Detects cycles and unknown dep references at build time.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable

from .models import TaskExecution, TaskSpec, TaskStatus

log = logging.getLogger(__name__)


class DAGError(Exception):
    pass


class TaskDAG:
    def __init__(self, specs: Iterable[TaskSpec]) -> None:
        self.specs: dict[str, TaskSpec] = {s.id: s for s in specs}
        # Forward edges: dep -> set of dependents
        self.dependents: dict[str, set[str]] = defaultdict(set)
        # Reverse edges: task -> deps
        self.deps: dict[str, set[str]] = {}

        self._build()
        self._validate()

    def _build(self) -> None:
        for tid, spec in self.specs.items():
            self.deps[tid] = set(spec.depends_on)
            for d in spec.depends_on:
                self.dependents[d].add(tid)

    def _validate(self) -> None:
        # Unknown deps
        for tid, deps in self.deps.items():
            unknown = deps - set(self.specs.keys())
            if unknown:
                raise DAGError(f"Task {tid!r} depends on unknown task(s): {sorted(unknown)}")

        # Cycle detection (Kahn's algorithm)
        indeg = {tid: len(deps) for tid, deps in self.deps.items()}
        queue = [tid for tid, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            tid = queue.pop()
            seen += 1
            for dep in self.dependents.get(tid, ()):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    queue.append(dep)
        if seen != len(self.specs):
            raise DAGError("Cycle detected in task dependencies")

    # ----- query methods -------------------------------------------------

    def get_ready(self, executions: dict[str, TaskExecution]) -> list[str]:
        """Tasks whose status is PENDING or READY and all deps are DONE."""
        ready: list[str] = []
        for tid, spec in self.specs.items():
            ex = executions.get(tid)
            if ex is None:
                continue
            if ex.status not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            if all(
                executions.get(d) is not None and executions[d].status == TaskStatus.DONE
                for d in spec.depends_on
            ):
                ready.append(tid)
        return ready

    def get_blocked(self, executions: dict[str, TaskExecution]) -> list[str]:
        """Tasks whose dep failed/blocked. They get marked BLOCKED."""
        blocked: list[str] = []
        for tid, spec in self.specs.items():
            ex = executions.get(tid)
            if ex is None or ex.status.is_terminal:
                continue
            for d in spec.depends_on:
                dep_ex = executions.get(d)
                if dep_ex and dep_ex.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                    blocked.append(tid)
                    break
        return blocked

    def all_terminal(self, executions: dict[str, TaskExecution]) -> bool:
        return all(
            executions.get(tid) is not None and executions[tid].status.is_terminal
            for tid in self.specs
        )

    def topo_order(self) -> list[str]:
        indeg = {tid: len(deps) for tid, deps in self.deps.items()}
        order: list[str] = []
        queue = sorted([tid for tid, d in indeg.items() if d == 0])
        while queue:
            tid = queue.pop(0)
            order.append(tid)
            for dep in sorted(self.dependents.get(tid, ())):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    queue.append(dep)
        return order

    def waiting_on(self, task_id: str, executions: dict[str, TaskExecution]) -> list[str]:
        """For UI/debug: which deps are not yet done."""
        spec = self.specs.get(task_id)
        if not spec:
            return []
        return [
            d
            for d in spec.depends_on
            if executions.get(d) is None or executions[d].status != TaskStatus.DONE
        ]
