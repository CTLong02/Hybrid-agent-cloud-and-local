"""Tests for `hybrid_agent.dag.TaskDAG`."""
from __future__ import annotations

import pytest

from hybrid_agent.dag import DAGError, TaskDAG
from hybrid_agent.models import TaskStatus


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------

class TestBuildAndValidate:
    def test_empty_dag_is_legal(self):
        dag = TaskDAG([])
        assert dag.specs == {}
        assert dag.topo_order() == []

    def test_unknown_dependency_raises(self, make_spec):
        with pytest.raises(DAGError, match="unknown task"):
            TaskDAG([make_spec("T1", deps=["T0"])])

    def test_self_loop_detected_as_cycle(self, make_spec):
        with pytest.raises(DAGError, match="Cycle"):
            TaskDAG([make_spec("T1", deps=["T1"])])

    def test_two_node_cycle_detected(self, make_spec):
        with pytest.raises(DAGError, match="Cycle"):
            TaskDAG([
                make_spec("A", deps=["B"]),
                make_spec("B", deps=["A"]),
            ])

    def test_three_node_cycle_detected(self, make_spec):
        with pytest.raises(DAGError, match="Cycle"):
            TaskDAG([
                make_spec("A", deps=["B"]),
                make_spec("B", deps=["C"]),
                make_spec("C", deps=["A"]),
            ])


# ---------------------------------------------------------------------------
# Topo order
# ---------------------------------------------------------------------------

class TestTopoOrder:
    def test_linear_chain(self, make_spec):
        dag = TaskDAG([
            make_spec("T3", deps=["T2"]),
            make_spec("T1"),
            make_spec("T2", deps=["T1"]),
        ])
        order = dag.topo_order()
        # Topological constraint: deps appear before dependents
        assert order.index("T1") < order.index("T2") < order.index("T3")

    def test_diamond(self, make_spec):
        # A -> B,C -> D
        dag = TaskDAG([
            make_spec("A"),
            make_spec("B", deps=["A"]),
            make_spec("C", deps=["A"]),
            make_spec("D", deps=["B", "C"]),
        ])
        order = dag.topo_order()
        assert order[0] == "A"
        assert order[-1] == "D"
        assert set(order[1:3]) == {"B", "C"}

    def test_topo_is_deterministic(self, make_spec):
        # Same inputs -> same output (Kahn's queue uses sorted)
        specs = [make_spec(f"T{i}") for i in range(5)]
        a = TaskDAG(specs).topo_order()
        b = TaskDAG(specs).topo_order()
        assert a == b


# ---------------------------------------------------------------------------
# get_ready / get_blocked
# ---------------------------------------------------------------------------

class TestReadiness:
    def test_pending_task_with_done_dep_is_ready(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B", deps=["A"])])
        execs = {
            "A": make_execution("A", status=TaskStatus.DONE),
            "B": make_execution("B", status=TaskStatus.PENDING),
        }
        assert dag.get_ready(execs) == ["B"]

    def test_pending_task_with_pending_dep_is_not_ready(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B", deps=["A"])])
        execs = {
            "A": make_execution("A", status=TaskStatus.PENDING),
            "B": make_execution("B", status=TaskStatus.PENDING),
        }
        assert "B" not in dag.get_ready(execs)

    def test_already_done_task_is_not_returned_as_ready(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A")])
        execs = {"A": make_execution("A", status=TaskStatus.DONE)}
        assert dag.get_ready(execs) == []

    def test_in_progress_task_is_not_ready(self, make_spec, make_execution):
        # Prevents the duplicate-scheduling bug
        dag = TaskDAG([make_spec("A")])
        execs = {"A": make_execution("A", status=TaskStatus.PLANNING)}
        assert dag.get_ready(execs) == []

    def test_blocked_when_dep_failed(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B", deps=["A"])])
        execs = {
            "A": make_execution("A", status=TaskStatus.FAILED),
            "B": make_execution("B", status=TaskStatus.PENDING),
        }
        assert dag.get_blocked(execs) == ["B"]

    def test_blocked_when_dep_blocked_propagates(self, make_spec, make_execution):
        # A failed -> B blocked -> C blocked too
        dag = TaskDAG([
            make_spec("A"),
            make_spec("B", deps=["A"]),
            make_spec("C", deps=["B"]),
        ])
        execs = {
            "A": make_execution("A", status=TaskStatus.FAILED),
            "B": make_execution("B", status=TaskStatus.BLOCKED),
            "C": make_execution("C", status=TaskStatus.PENDING),
        }
        assert "C" in dag.get_blocked(execs)

    def test_terminal_tasks_not_blocked_again(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B", deps=["A"])])
        execs = {
            "A": make_execution("A", status=TaskStatus.FAILED),
            "B": make_execution("B", status=TaskStatus.BLOCKED),
        }
        # B already terminal — get_blocked shouldn't re-include it
        assert "B" not in dag.get_blocked(execs)


# ---------------------------------------------------------------------------
# all_terminal / waiting_on
# ---------------------------------------------------------------------------

class TestQueries:
    def test_all_terminal_when_every_task_is_done(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B")])
        execs = {
            "A": make_execution("A", status=TaskStatus.DONE),
            "B": make_execution("B", status=TaskStatus.FAILED),
        }
        assert dag.all_terminal(execs) is True

    def test_all_terminal_false_when_one_pending(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A"), make_spec("B")])
        execs = {
            "A": make_execution("A", status=TaskStatus.DONE),
            "B": make_execution("B", status=TaskStatus.PENDING),
        }
        assert dag.all_terminal(execs) is False

    def test_waiting_on_returns_unsatisfied_deps(self, make_spec, make_execution):
        dag = TaskDAG([
            make_spec("A"),
            make_spec("B"),
            make_spec("C", deps=["A", "B"]),
        ])
        execs = {
            "A": make_execution("A", status=TaskStatus.DONE),
            "B": make_execution("B", status=TaskStatus.PENDING),
            "C": make_execution("C", status=TaskStatus.PENDING),
        }
        assert dag.waiting_on("C", execs) == ["B"]

    def test_waiting_on_unknown_task_returns_empty(self, make_spec, make_execution):
        dag = TaskDAG([make_spec("A")])
        assert dag.waiting_on("ZZ", {"A": make_execution("A")}) == []
