"""Pure, atomic reducers for incremental task-plan commands."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, cast

from firstcoder.planning.models import (
    Task,
    TaskPlan,
    TaskPlanError,
    TaskPlanMode,
    TaskStatus,
)
from firstcoder.planning.validation import validate_plan

_VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_VALID_MODES = frozenset({"linear", "dag"})


@dataclass(frozen=True, slots=True)
class Unset:
    """Sentinel type used when a nullable field was not supplied."""


UNSET = Unset()


@dataclass(frozen=True, slots=True)
class TaskPatch:
    id: str
    status: TaskStatus | None = None
    owner: str | None | Unset = UNSET
    add_depends_on: tuple[str, ...] = ()
    remove_depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskRevision:
    id: str
    content: str


@dataclass(frozen=True, slots=True)
class ReductionResult:
    plan: TaskPlan
    changes: tuple[dict[str, object], ...]
    changed: bool


class TaskPlanCommandError(ValueError):
    """Raised when an incremental task-plan command is invalid."""


class TaskPlanRevisionConflict(TaskPlanCommandError):
    """Raised when a command was based on an outdated plan revision."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"task plan revision conflict: expected {expected}, actual {actual}")


def create_tasks(
    *,
    current_plan: TaskPlan | None,
    expected_revision: int,
    mode: TaskPlanMode | str,
    tasks: object,
) -> ReductionResult:
    """Create the first plan or append new tasks to the current plan."""

    actual_revision = current_plan.revision if current_plan is not None else 0
    _require_revision(expected_revision, actual_revision)
    normalized_mode = _parse_mode(mode)
    raw_tasks = _require_list(tasks, field="tasks")

    if current_plan is not None and normalized_mode != current_plan.mode:
        raise TaskPlanCommandError(f"cannot switch task plan mode from {current_plan.mode} to {normalized_mode}")
    if current_plan is None and not raw_tasks:
        raise TaskPlanCommandError("initial task plan must contain at least one task")

    existing_tasks = current_plan.tasks if current_plan is not None else ()
    existing_ids = {task.id for task in existing_tasks}
    next_order = max((task.order for task in existing_tasks), default=-1) + 1
    additions: list[Task] = []

    for index, item in enumerate(raw_tasks):
        task = _parse_created_task(item, field=f"tasks[{index}]")
        if task.id in existing_ids:
            raise TaskPlanCommandError(f"duplicate task id: {task.id}")
        existing_ids.add(task.id)
        additions.append(replace(task, order=next_order + index))

    if current_plan is not None and not additions:
        return ReductionResult(plan=current_plan, changes=(), changed=False)

    candidate = TaskPlan(
        mode=normalized_mode,
        revision=actual_revision + 1,
        tasks=existing_tasks + tuple(additions),
    )
    _validate_candidate(candidate)
    return ReductionResult(
        plan=candidate,
        changes=tuple(task.to_dict() for task in additions),
        changed=True,
    )


def update_tasks(
    *,
    plan: TaskPlan,
    expected_revision: int,
    updates: object,
) -> ReductionResult:
    """Atomically update task status, owner, and dependency edges by ID."""

    _require_revision(expected_revision, plan.revision)
    raw_updates = _require_list(updates, field="updates")
    patches = tuple(_parse_task_patch(item, field=f"updates[{index}]") for index, item in enumerate(raw_updates))
    _require_unique_command_ids(patches)

    task_by_id = {task.id: task for task in plan.tasks}
    replacements: dict[str, Task] = {}
    changes: list[dict[str, object]] = []

    for patch in patches:
        original = replacements.get(patch.id, task_by_id.get(patch.id))
        if original is None:
            raise TaskPlanCommandError(f"unknown task id: {patch.id}")

        task = original
        change: dict[str, object] = {"id": patch.id}
        if patch.status is not None and patch.status != task.status:
            task = replace(task, status=patch.status)
            change["status"] = patch.status
        if not isinstance(patch.owner, Unset) and patch.owner != task.owner:
            task = replace(task, owner=patch.owner)
            change["owner"] = patch.owner

        dependencies = list(task.depends_on)
        removed = [entry for entry in patch.remove_depends_on if entry in dependencies]
        if removed:
            remove_set = set(removed)
            dependencies = [entry for entry in dependencies if entry not in remove_set]
            change["remove_depends_on"] = removed
        added = [entry for entry in patch.add_depends_on if entry not in dependencies]
        if added:
            dependencies.extend(added)
            change["add_depends_on"] = added
        if tuple(dependencies) != task.depends_on:
            task = replace(task, depends_on=tuple(dependencies))

        if task != original:
            replacements[patch.id] = task
            changes.append(change)

    if not changes:
        return ReductionResult(plan=plan, changes=(), changed=False)

    return _changed_result(plan, replacements, changes)


def revise_tasks(
    *,
    plan: TaskPlan,
    expected_revision: int,
    revisions: object,
) -> ReductionResult:
    """Atomically revise task content by ID."""

    _require_revision(expected_revision, plan.revision)
    raw_revisions = _require_list(revisions, field="revisions")
    parsed = tuple(_parse_task_revision(item, field=f"revisions[{index}]") for index, item in enumerate(raw_revisions))
    _require_unique_command_ids(parsed)

    task_by_id = {task.id: task for task in plan.tasks}
    replacements: dict[str, Task] = {}
    changes: list[dict[str, object]] = []
    for revision in parsed:
        task = task_by_id.get(revision.id)
        if task is None:
            raise TaskPlanCommandError(f"unknown task id: {revision.id}")
        if task.content != revision.content:
            replacements[revision.id] = replace(task, content=revision.content)
            changes.append({"id": revision.id, "content": revision.content})

    if not changes:
        return ReductionResult(plan=plan, changes=(), changed=False)

    return _changed_result(plan, replacements, changes)


def _parse_created_task(value: object, *, field: str) -> Task:
    if isinstance(value, Task):
        try:
            return Task.from_dict(value.to_dict())
        except TaskPlanError as error:
            raise TaskPlanCommandError(f"{field}: {error}") from error
    payload = _require_mapping(value, field=field)
    _reject_unknown_fields(
        payload,
        allowed={"id", "content", "status", "depends_on", "owner"},
        field=field,
    )
    try:
        return Task.from_dict(payload)
    except TaskPlanError as error:
        raise TaskPlanCommandError(f"{field}: {error}") from error


def _changed_result(
    plan: TaskPlan,
    replacements: Mapping[str, Task],
    changes: list[dict[str, object]],
) -> ReductionResult:
    candidate = TaskPlan(
        mode=plan.mode,
        revision=plan.revision + 1,
        tasks=tuple(replacements.get(task.id, task) for task in plan.tasks),
    )
    _validate_candidate(candidate)
    return ReductionResult(plan=candidate, changes=tuple(changes), changed=True)


def _parse_task_patch(value: object, *, field: str) -> TaskPatch:
    if isinstance(value, TaskPatch):
        _validate_patch(value, field=field)
        return value
    payload = _require_mapping(value, field=field)
    _reject_unknown_fields(
        payload,
        allowed={"id", "status", "owner", "add_depends_on", "remove_depends_on"},
        field=field,
    )
    patch = TaskPatch(
        id=_require_non_blank_string(payload.get("id"), field=f"{field}.id"),
        status=_parse_optional_status(payload.get("status"), field=f"{field}.status"),
        owner=_parse_owner(payload["owner"], field=f"{field}.owner") if "owner" in payload else UNSET,
        add_depends_on=_parse_string_sequence(payload.get("add_depends_on", []), field=f"{field}.add_depends_on"),
        remove_depends_on=_parse_string_sequence(payload.get("remove_depends_on", []), field=f"{field}.remove_depends_on"),
    )
    _validate_patch(patch, field=field)
    return patch


def _validate_patch(patch: TaskPatch, *, field: str) -> None:
    _require_non_blank_string(patch.id, field=f"{field}.id")
    _parse_optional_status(patch.status, field=f"{field}.status")
    if not isinstance(patch.owner, Unset):
        _parse_owner(patch.owner, field=f"{field}.owner")
    additions = _parse_string_sequence(patch.add_depends_on, field=f"{field}.add_depends_on")
    removals = _parse_string_sequence(patch.remove_depends_on, field=f"{field}.remove_depends_on")
    overlap = set(additions).intersection(removals)
    if overlap:
        dependency = next(entry for entry in additions if entry in overlap)
        raise TaskPlanCommandError(f"{field} cannot add and remove dependency {dependency!r}")


def _parse_task_revision(value: object, *, field: str) -> TaskRevision:
    if isinstance(value, TaskRevision):
        return TaskRevision(
            id=_require_non_blank_string(value.id, field=f"{field}.id"),
            content=_require_non_blank_string(value.content, field=f"{field}.content"),
        )
    payload = _require_mapping(value, field=field)
    _reject_unknown_fields(payload, allowed={"id", "content"}, field=field)
    return TaskRevision(
        id=_require_non_blank_string(payload.get("id"), field=f"{field}.id"),
        content=_require_non_blank_string(payload.get("content"), field=f"{field}.content"),
    )


def _require_revision(expected: object, actual: int) -> None:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise TaskPlanCommandError("expected_revision must be a non-negative integer")
    if expected != actual:
        raise TaskPlanRevisionConflict(expected=expected, actual=actual)


def _parse_mode(value: object) -> TaskPlanMode:
    if not isinstance(value, str) or value not in _VALID_MODES:
        raise TaskPlanCommandError(f"unknown task plan mode: {value!r}")
    return cast(TaskPlanMode, value)


def _parse_optional_status(value: object, *, field: str) -> TaskStatus | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _VALID_STATUSES:
        raise TaskPlanCommandError(f"{field} has unknown task status: {value!r}")
    return cast(TaskStatus, value)


def _parse_owner(value: object, *, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TaskPlanCommandError(f"{field} must be a string or null")
    return cast(str | None, value)


def _parse_string_sequence(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TaskPlanCommandError(f"{field} must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for entry in value:
        normalized = _require_non_blank_string(entry, field=field)
        if normalized in seen:
            raise TaskPlanCommandError(f"{field} repeats value {normalized!r}")
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _require_non_blank_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskPlanCommandError(f"{field} must be a non-blank string")
    return value


def _require_list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TaskPlanCommandError(f"{field} must be a list")
    return value


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TaskPlanCommandError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TaskPlanCommandError(f"{field} field names must be strings")
    return cast(Mapping[str, object], value)


def _reject_unknown_fields(payload: Mapping[str, object], *, allowed: set[str], field: str) -> None:
    unknown = [key for key in payload if key not in allowed]
    if unknown:
        raise TaskPlanCommandError(f"{field} has unknown field: {unknown[0]}")


def _require_unique_command_ids(commands: tuple[object, ...]) -> None:
    seen: set[str] = set()
    for command in commands:
        command_id = cast(str, getattr(command, "id"))
        if command_id in seen:
            raise TaskPlanCommandError(f"duplicate task id in command: {command_id}")
        seen.add(command_id)


def _validate_candidate(plan: TaskPlan) -> None:
    try:
        validate_plan(plan)
    except TaskPlanError as error:
        raise TaskPlanCommandError(str(error)) from error
