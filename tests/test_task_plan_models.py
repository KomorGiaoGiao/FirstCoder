from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from firstcoder.planning.models import Task, TaskPlan, TaskPlanError


def test_task_defaults_and_models_are_frozen() -> None:
    task = Task(id="inspect", content="Inspect the current implementation")
    plan = TaskPlan(mode="linear", revision=0, tasks=(task,))

    assert task.status == "pending"
    assert task.depends_on == ()
    assert task.owner is None
    assert task.order == 0

    with pytest.raises(FrozenInstanceError):
        task.status = "completed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.revision = 1  # type: ignore[misc]


def test_task_plan_json_round_trip_is_stable() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=3,
        tasks=(
            Task(id="inspect", content="Inspect", status="completed", order=0),
            Task(
                id="implement",
                content="Implement",
                status="in_progress",
                depends_on=("inspect",),
                owner="main",
                order=1,
            ),
        ),
    )

    encoded = json.dumps(plan.to_dict())
    restored = TaskPlan.from_dict(json.loads(encoded))

    assert restored == plan
    assert restored.to_dict() == {
        "mode": "dag",
        "revision": 3,
        "tasks": [
            {
                "id": "inspect",
                "content": "Inspect",
                "status": "completed",
                "depends_on": [],
                "owner": None,
                "order": 0,
            },
            {
                "id": "implement",
                "content": "Implement",
                "status": "in_progress",
                "depends_on": ["inspect"],
                "owner": "main",
                "order": 1,
            },
        ],
    }


def test_from_dict_normalizes_input_lists_to_tuples() -> None:
    plan = TaskPlan.from_dict(
        {
            "mode": "dag",
            "revision": 0,
            "tasks": [
                {
                    "id": "implement",
                    "content": "Implement",
                    "depends_on": ["inspect"],
                }
            ],
        }
    )

    assert isinstance(plan.tasks, tuple)
    assert isinstance(plan.tasks[0].depends_on, tuple)
    assert plan.tasks[0].depends_on == ("inspect",)


def test_from_dict_rejects_duplicate_task_ids() -> None:
    with pytest.raises(TaskPlanError, match="duplicate"):
        TaskPlan.from_dict(
            {
                "mode": "linear",
                "revision": 0,
                "tasks": [
                    {"id": "same", "content": "First"},
                    {"id": "same", "content": "Second"},
                ],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", ""), ("id", "   "), ("content", ""), ("content", "\t")],
)
def test_task_from_dict_rejects_blank_id_or_content(field: str, value: str) -> None:
    payload = {"id": "task-1", "content": "Do work"}
    payload[field] = value

    with pytest.raises(TaskPlanError, match=field):
        Task.from_dict(payload)


@pytest.mark.parametrize("mode", ["", "graph", "LINEAR"])
def test_task_plan_from_dict_rejects_unknown_mode(mode: str) -> None:
    with pytest.raises(TaskPlanError, match="mode"):
        TaskPlan.from_dict({"mode": mode, "revision": 0, "tasks": []})


@pytest.mark.parametrize("status", ["done", "ready", "failed"])
def test_task_from_dict_rejects_unknown_status(status: str) -> None:
    with pytest.raises(TaskPlanError, match="status"):
        Task.from_dict({"id": "task-1", "content": "Do work", "status": status})


@pytest.mark.parametrize("revision", [-1, True, 1.5, "1", None])
def test_task_plan_from_dict_rejects_invalid_revision(revision: object) -> None:
    with pytest.raises(TaskPlanError, match="revision"):
        TaskPlan.from_dict({"mode": "linear", "revision": revision, "tasks": []})


@pytest.mark.parametrize("tasks", [None, {}, "tasks", ["not-an-object"]])
def test_task_plan_from_dict_rejects_non_object_task_lists(tasks: object) -> None:
    with pytest.raises(TaskPlanError, match="tasks"):
        TaskPlan.from_dict({"mode": "linear", "revision": 0, "tasks": tasks})
