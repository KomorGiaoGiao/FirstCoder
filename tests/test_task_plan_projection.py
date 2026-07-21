from __future__ import annotations

from firstcoder.planning.models import Task, TaskPlan
from firstcoder.planning.projection import (
    blocked_task_ids,
    project_plan,
    ready_task_ids,
    topological_levels,
)


def test_dag_projection_derives_ready_blocked_and_stable_levels() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=4,
        tasks=(
            Task(id="b", content="B", status="pending", order=20),
            Task(id="a", content="A", status="completed", order=10),
            Task(id="d", content="D", depends_on=("b",), order=40),
            Task(id="c", content="C", depends_on=("a",), order=30),
        ),
    )

    assert ready_task_ids(plan) == ("b", "c")
    assert blocked_task_ids(plan) == ("d",)
    assert topological_levels(plan) == (("a", "b"), ("c", "d"))
    assert project_plan(plan) == {
        "mode": "dag",
        "revision": 4,
        "ready_task_ids": ["b", "c"],
        "blocked_task_ids": ["d"],
        "topological_levels": [["a", "b"], ["c", "d"]],
        "tasks": [task.to_dict() for task in plan.tasks],
    }


def test_cancelled_dependency_keeps_task_blocked() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=1,
        tasks=(
            Task(id="setup", content="Setup", status="cancelled"),
            Task(id="work", content="Work", depends_on=("setup",)),
        ),
    )

    assert ready_task_ids(plan) == ()
    assert blocked_task_ids(plan) == ("work",)


def test_linear_projection_uses_order_and_implicit_predecessors() -> None:
    plan = TaskPlan(
        mode="linear",
        revision=2,
        tasks=(
            Task(id="third", content="Third", order=30),
            Task(id="first", content="First", status="completed", order=10),
            Task(id="second", content="Second", status="pending", order=20),
        ),
    )

    assert ready_task_ids(plan) == ("second",)
    assert blocked_task_ids(plan) == ("third",)
    assert topological_levels(plan) == (("first",), ("second",), ("third",))


def test_terminal_tasks_are_neither_ready_nor_blocked() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=1,
        tasks=(
            Task(id="done", content="Done", status="completed"),
            Task(id="skipped", content="Skipped", status="cancelled"),
        ),
    )

    assert ready_task_ids(plan) == ()
    assert blocked_task_ids(plan) == ()


def test_stable_order_uses_declaration_order_to_break_ties() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=1,
        tasks=(
            Task(id="second_declared", content="Second declared", order=10),
            Task(id="first_declared", content="First declared", order=10),
        ),
    )

    assert ready_task_ids(plan) == ("second_declared", "first_declared")
    assert topological_levels(plan) == (("second_declared", "first_declared"),)


def test_linear_cancelled_predecessor_permanently_blocks_later_tasks() -> None:
    plan = TaskPlan(
        mode="linear",
        revision=1,
        tasks=(
            Task(id="cancelled", content="Cancelled", status="cancelled", order=10),
            Task(id="next", content="Next", order=20),
            Task(id="last", content="Last", order=30),
        ),
    )

    assert ready_task_ids(plan) == ()
    assert blocked_task_ids(plan) == ("next", "last")
