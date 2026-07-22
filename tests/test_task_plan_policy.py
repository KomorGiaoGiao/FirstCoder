"""Unified task-plan creation and final reconciliation policy tests."""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.agent.task_plan_policy import TaskPlanPolicy
from firstcoder.planning.models import Task, TaskPlan


@dataclass
class _View:
    task_plan: TaskPlan | None


class _Session:
    def __init__(self, plan: TaskPlan | None) -> None:
        self.plan = plan

    def rebuild_view(self) -> _View:
        return _View(task_plan=self.plan)


def test_linear_plan_reconciliation_lists_unfinished_tasks_in_stable_order() -> None:
    plan = TaskPlan(
        mode="linear",
        revision=3,
        tasks=(
            Task(id="document", content="Document blocker", status="in_progress", order=30),
            Task(id="inspect", content="Inspect implementation", status="completed", order=10),
            Task(id="verify", content="Run focused tests", status="pending", order=20),
        ),
    )

    instruction = TaskPlanPolicy(_Session(plan)).final_reconciliation_instruction()

    assert isinstance(instruction, str)
    assert "linear" in instruction
    assert "[pending] verify: Run focused tests" in instruction
    assert "[in_progress] document: Document blocker" in instruction
    assert "inspect" not in instruction
    assert instruction.index("verify") < instruction.index("document")
    assert "task_update" in instruction
    assert "by task ID" in instruction
    assert "do not recreate or rebuild" in instruction.lower()
    assert "real blocker" in instruction.lower()
    assert "do not claim completion" in instruction.lower()


def test_dag_plan_uses_the_same_reconciliation_protocol() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=7,
        tasks=(
            Task(id="research", content="Research API", status="in_progress", order=10),
            Task(
                id="implement",
                content="Implement adapter",
                status="pending",
                depends_on=("research",),
                order=20,
            ),
            Task(id="obsolete", content="Old path", status="cancelled", order=30),
        ),
    )

    instruction = TaskPlanPolicy(_Session(plan)).final_reconciliation_instruction()

    assert isinstance(instruction, str)
    assert "dag" in instruction
    assert "[in_progress] research: Research API" in instruction
    assert "[pending] implement: Implement adapter" in instruction
    assert "obsolete" not in instruction
    assert "task_update" in instruction
    assert "do not recreate or rebuild" in instruction.lower()


def test_terminal_or_missing_plan_needs_no_final_reconciliation() -> None:
    terminal = TaskPlan(
        mode="dag",
        revision=2,
        tasks=(
            Task(id="done", content="Done", status="completed"),
            Task(id="skipped", content="Skipped", status="cancelled"),
        ),
    )

    assert TaskPlanPolicy(_Session(None)).final_reconciliation_instruction() is None
    assert TaskPlanPolicy(_Session(terminal)).final_reconciliation_instruction() is None
