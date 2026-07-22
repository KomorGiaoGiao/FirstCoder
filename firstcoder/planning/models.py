"""Canonical task-plan domain models and stable JSON serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, cast

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPlanMode = Literal["linear", "dag"]

_TASK_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed", "cancelled"})
_TASK_PLAN_MODES: frozenset[str] = frozenset({"linear", "dag"})


class TaskPlanError(ValueError):
    """Raised when a serialized task plan is structurally invalid."""


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    content: str
    status: TaskStatus = "pending"
    depends_on: tuple[str, ...] = ()
    owner: str | None = None
    order: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "owner": self.owner,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Task":
        if not isinstance(payload, Mapping):
            raise TaskPlanError("task must be an object")

        task_id = _require_non_blank_string(payload.get("id"), field="id")
        content = _require_non_blank_string(payload.get("content"), field="content")
        status = _require_status(payload.get("status", "pending"))
        depends_on = _require_string_list(payload.get("depends_on", []), field="depends_on")
        owner = _require_optional_string(payload.get("owner"), field="owner")
        order = _require_non_negative_int(payload.get("order", 0), field="order")
        return cls(
            id=task_id,
            content=content,
            status=status,
            depends_on=depends_on,
            owner=owner,
            order=order,
        )


@dataclass(frozen=True, slots=True)
class TaskPlan:
    mode: TaskPlanMode
    revision: int
    tasks: tuple[Task, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "revision": self.revision,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TaskPlan":
        if not isinstance(payload, Mapping):
            raise TaskPlanError("task plan must be an object")

        mode = _require_mode(payload.get("mode"))
        revision = _require_non_negative_int(payload.get("revision"), field="revision")
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            raise TaskPlanError("tasks must be a list of objects")

        tasks: list[Task] = []
        for index, item in enumerate(raw_tasks):
            if not isinstance(item, Mapping):
                raise TaskPlanError(f"tasks[{index}] must be an object")
            tasks.append(Task.from_dict(item))
        _require_unique_task_ids(tasks)
        return cls(mode=mode, revision=revision, tasks=tuple(tasks))


def _require_non_blank_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskPlanError(f"{field} must be a non-blank string")
    return value


def _require_optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskPlanError(f"{field} must be a string or null")
    return value


def _require_non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskPlanError(f"{field} must be a non-negative integer")
    return value


def _require_mode(value: object) -> TaskPlanMode:
    if not isinstance(value, str) or value not in _TASK_PLAN_MODES:
        raise TaskPlanError(f"unknown task plan mode: {value!r}")
    return cast(TaskPlanMode, value)


def _require_status(value: object) -> TaskStatus:
    if not isinstance(value, str) or value not in _TASK_STATUSES:
        raise TaskPlanError(f"unknown task status: {value!r}")
    return cast(TaskStatus, value)


def _require_string_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TaskPlanError(f"{field} must be a list of strings")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise TaskPlanError(f"{field} must be a list of non-blank strings")
        result.append(entry)
    return tuple(result)


def _require_unique_task_ids(tasks: list[Task]) -> None:
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise TaskPlanError(f"duplicate task id: {task.id}")
        seen.add(task.id)
