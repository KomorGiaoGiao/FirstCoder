from __future__ import annotations

from dataclasses import fields

import pytest

from firstcoder.planning.models import Task, TaskPlan
from firstcoder.planning.reducer import (
    TaskPatch,
    TaskPlanCommandError,
    TaskPlanRevisionConflict,
    create_tasks,
    revise_tasks,
    update_tasks,
)


def _linear_plan(*tasks: Task, revision: int = 1) -> TaskPlan:
    return TaskPlan(mode="linear", revision=revision, tasks=tasks)


def test_create_initial_plan_assigns_stable_order_and_one_revision() -> None:
    result = create_tasks(
        current_plan=None,
        expected_revision=0,
        mode="linear",
        tasks=[
            {"id": "inspect", "content": "Inspect"},
            {"id": "implement", "content": "Implement"},
        ],
    )

    assert result.changed is True
    assert result.plan == _linear_plan(
        Task(id="inspect", content="Inspect", order=0),
        Task(id="implement", content="Implement", order=1),
    )
    assert result.changes == (
        {"id": "inspect", "content": "Inspect", "status": "pending", "depends_on": [], "owner": None, "order": 0},
        {"id": "implement", "content": "Implement", "status": "pending", "depends_on": [], "owner": None, "order": 1},
    )


def test_create_appends_task_objects_without_replacing_existing_tasks() -> None:
    existing = _linear_plan(Task(id="inspect", content="Inspect", order=7), revision=4)

    result = create_tasks(
        current_plan=existing,
        expected_revision=4,
        mode="linear",
        tasks=[Task(id="test", content="Test", order=0)],
    )

    assert result.plan.revision == 5
    assert result.plan.tasks[0] is existing.tasks[0]
    assert [(task.id, task.order) for task in result.plan.tasks] == [("inspect", 7), ("test", 8)]
    assert result.changes == ({"id": "test", "content": "Test", "status": "pending", "depends_on": [], "owner": None, "order": 8},)


def test_create_rejects_mode_switch_and_duplicate_ids() -> None:
    plan = _linear_plan(Task(id="a", content="A"))

    with pytest.raises(TaskPlanCommandError, match="mode"):
        create_tasks(
            current_plan=plan,
            expected_revision=1,
            mode="dag",
            tasks=[{"id": "b", "content": "B"}],
        )
    with pytest.raises(TaskPlanCommandError, match="duplicate"):
        create_tasks(
            current_plan=plan,
            expected_revision=1,
            mode="linear",
            tasks=[{"id": "a", "content": "Again"}],
        )


def test_update_applies_batch_status_transition_atomically() -> None:
    plan = _linear_plan(
        Task(id="a", content="A", status="in_progress", order=0),
        Task(id="b", content="B", order=1),
    )

    result = update_tasks(
        plan=plan,
        expected_revision=1,
        updates=[
            {"id": "a", "status": "completed"},
            TaskPatch(id="b", status="in_progress"),
        ],
    )

    assert result.plan.revision == 2
    assert [task.status for task in result.plan.tasks] == ["completed", "in_progress"]
    assert result.changes == (
        {"id": "a", "status": "completed"},
        {"id": "b", "status": "in_progress"},
    )


def test_update_sets_and_clears_owner_with_unset_distinct_from_null() -> None:
    plan = _linear_plan(Task(id="work", content="Work"))
    assigned = update_tasks(
        plan=plan,
        expected_revision=1,
        updates=[{"id": "work", "owner": "agent-1"}],
    )
    unchanged_owner = update_tasks(
        plan=assigned.plan,
        expected_revision=2,
        updates=[TaskPatch(id="work", status="in_progress")],
    )
    cleared = update_tasks(
        plan=unchanged_owner.plan,
        expected_revision=3,
        updates=[{"id": "work", "owner": None}],
    )

    assert assigned.plan.tasks[0].owner == "agent-1"
    assert unchanged_owner.plan.tasks[0].owner == "agent-1"
    assert cleared.plan.tasks[0].owner is None
    assert cleared.changes == ({"id": "work", "owner": None},)


def test_update_adds_and_removes_dependencies_incrementally() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=6,
        tasks=(
            Task(id="a", content="A"),
            Task(id="b", content="B"),
            Task(id="work", content="Work", depends_on=("a",)),
        ),
    )

    result = update_tasks(
        plan=plan,
        expected_revision=6,
        updates=[
            {
                "id": "work",
                "add_depends_on": ["b"],
                "remove_depends_on": ["a"],
            }
        ],
    )

    assert result.plan.revision == 7
    assert result.plan.tasks[2].depends_on == ("b",)
    assert result.changes == ({"id": "work", "add_depends_on": ["b"], "remove_depends_on": ["a"]},)


def test_revise_tasks_is_the_dedicated_content_operation() -> None:
    plan = _linear_plan(Task(id="docs", content="Write docs"), revision=9)

    result = revise_tasks(
        plan=plan,
        expected_revision=9,
        revisions=[{"id": "docs", "content": "Update English and Chinese docs"}],
    )

    assert result.plan.revision == 10
    assert result.plan.tasks[0].content == "Update English and Chinese docs"
    assert result.changes == ({"id": "docs", "content": "Update English and Chinese docs"},)


def test_task_patch_protocol_has_no_content_and_rejects_it_clearly() -> None:
    assert "content" not in {field.name for field in fields(TaskPatch)}

    with pytest.raises(TypeError, match="content"):
        TaskPatch(id="work", content="Rewrite")  # type: ignore[call-arg]
    with pytest.raises(TaskPlanCommandError, match="content"):
        update_tasks(
            plan=_linear_plan(Task(id="work", content="Work")),
            expected_revision=1,
            updates=[{"id": "work", "content": "Rewrite"}],
        )


def test_revision_conflict_carries_expected_and_actual() -> None:
    plan = _linear_plan(Task(id="work", content="Work"), revision=5)

    with pytest.raises(TaskPlanRevisionConflict) as caught:
        update_tasks(plan=plan, expected_revision=4, updates=[])

    assert caught.value.expected == 4
    assert caught.value.actual == 5


@pytest.mark.parametrize("operation", ["update", "revise"])
def test_unknown_task_id_rejects_entire_command(operation: str) -> None:
    plan = _linear_plan(Task(id="work", content="Work"))

    with pytest.raises(TaskPlanCommandError, match="unknown task id: missing"):
        if operation == "update":
            update_tasks(
                plan=plan,
                expected_revision=1,
                updates=[{"id": "missing", "status": "completed"}],
            )
        else:
            revise_tasks(
                plan=plan,
                expected_revision=1,
                revisions=[{"id": "missing", "content": "Missing"}],
            )


def test_invalid_update_batch_rolls_back_without_mutating_original() -> None:
    plan = TaskPlan(
        mode="dag",
        revision=2,
        tasks=(Task(id="a", content="A"), Task(id="b", content="B")),
    )
    before = plan.to_dict()

    with pytest.raises(TaskPlanCommandError, match="missing"):
        update_tasks(
            plan=plan,
            expected_revision=2,
            updates=[
                {"id": "a", "owner": "agent-1"},
                {"id": "b", "add_depends_on": ["missing"]},
            ],
        )

    assert plan.to_dict() == before
    assert plan.revision == 2
    assert plan.tasks[0].owner is None


@pytest.mark.parametrize("operation", ["create", "update", "revise"])
def test_no_op_returns_same_plan_object_without_revision_increment(operation: str) -> None:
    plan = _linear_plan(Task(id="work", content="Work", owner="main"), revision=3)

    if operation == "create":
        result = create_tasks(
            current_plan=plan,
            expected_revision=3,
            mode="linear",
            tasks=[],
        )
    elif operation == "update":
        result = update_tasks(
            plan=plan,
            expected_revision=3,
            updates=[{"id": "work", "owner": "main", "add_depends_on": []}],
        )
    else:
        result = revise_tasks(
            plan=plan,
            expected_revision=3,
            revisions=[{"id": "work", "content": "Work"}],
        )

    assert result.plan is plan
    assert result.changed is False
    assert result.changes == ()
    assert result.plan.revision == 3


def test_raw_inputs_reject_unknown_fields_and_non_lists() -> None:
    plan = _linear_plan(Task(id="work", content="Work"))

    with pytest.raises(TaskPlanCommandError, match="unknown field"):
        update_tasks(
            plan=plan,
            expected_revision=1,
            updates=[{"id": "work", "ready": True}],
        )
    with pytest.raises(TaskPlanCommandError, match="list"):
        revise_tasks(
            plan=plan,
            expected_revision=1,
            revisions={"id": "work", "content": "Rewrite"},
        )
