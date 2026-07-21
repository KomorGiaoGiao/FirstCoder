from __future__ import annotations

import pytest

from firstcoder.planning.models import Task, TaskPlan, TaskPlanError
from firstcoder.planning.validation import validate_plan


def _plan(*tasks: Task, mode: str = "dag") -> TaskPlan:
    return TaskPlan(mode=mode, revision=1, tasks=tasks)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "task",
    [
        Task(id="work", content="Work", depends_on=("missing",)),
        Task(id="work", content="Work", depends_on=("work",)),
        Task(id="work", content="Work", depends_on=("setup", "setup")),
    ],
)
def test_validate_plan_rejects_invalid_dependency_references(task: Task) -> None:
    tasks = (Task(id="setup", content="Setup"), task) if task.id != "setup" else (task,)

    with pytest.raises(TaskPlanError):
        validate_plan(_plan(*tasks))


def test_validate_plan_rejects_dag_cycle() -> None:
    with pytest.raises(TaskPlanError, match="cycle"):
        validate_plan(
            _plan(
                Task(id="a", content="A", depends_on=("b",)),
                Task(id="b", content="B", depends_on=("a",)),
            )
        )


def test_validate_plan_rejects_multiple_linear_tasks_in_progress() -> None:
    with pytest.raises(TaskPlanError, match="in_progress"):
        validate_plan(
            _plan(
                Task(id="a", content="A", status="in_progress", order=0),
                Task(id="b", content="B", status="in_progress", order=1),
                mode="linear",
            )
        )


@pytest.mark.parametrize("mode", ["linear", "dag"])
def test_validate_plan_rejects_start_before_prerequisites(mode: str) -> None:
    first = Task(id="first", content="First", order=0)
    second = Task(
        id="second",
        content="Second",
        status="in_progress",
        depends_on=("first",) if mode == "dag" else (),
        order=1,
    )

    with pytest.raises(TaskPlanError, match="ready"):
        validate_plan(_plan(first, second, mode=mode))


def test_validate_plan_allows_parallel_ready_dag_tasks() -> None:
    validate_plan(
        _plan(
            Task(id="a", content="A", status="in_progress"),
            Task(id="b", content="B", status="in_progress"),
            Task(id="join", content="Join", depends_on=("a", "b")),
        )
    )


def test_cancelled_dependency_does_not_satisfy_dependent_task() -> None:
    with pytest.raises(TaskPlanError, match="ready"):
        validate_plan(
            _plan(
                Task(id="setup", content="Setup", status="cancelled"),
                Task(
                    id="work",
                    content="Work",
                    status="in_progress",
                    depends_on=("setup",),
                ),
            )
        )


def test_linear_readiness_uses_order_instead_of_explicit_dependencies() -> None:
    validate_plan(
        _plan(
            Task(
                id="first",
                content="First",
                status="in_progress",
                depends_on=("second",),
                order=10,
            ),
            Task(id="second", content="Second", order=20),
            mode="linear",
        )
    )
