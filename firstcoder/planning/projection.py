"""Pure derived views for canonical task plans."""

from __future__ import annotations

from firstcoder.planning.models import Task, TaskPlan, TaskPlanError

_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})


def ordered_tasks(plan: TaskPlan) -> tuple[Task, ...]:
    """Return tasks in stable display order."""

    indexed = enumerate(plan.tasks)
    return tuple(task for _, task in sorted(indexed, key=lambda item: (item[1].order, item[0])))


def effective_dependencies(plan: TaskPlan) -> dict[str, tuple[str, ...]]:
    """Return explicit DAG edges or implicit linear predecessor edges."""

    if plan.mode == "dag":
        return {task.id: task.depends_on for task in plan.tasks}

    ordered = ordered_tasks(plan)
    return {task.id: tuple(previous.id for previous in ordered[:index]) for index, task in enumerate(ordered)}


def ready_task_ids(plan: TaskPlan) -> tuple[str, ...]:
    tasks = ordered_tasks(plan)
    task_by_id = {task.id: task for task in tasks}
    dependencies = effective_dependencies(plan)
    return tuple(task.id for task in tasks if task.status == "pending" and all(task_by_id[dependency].status == "completed" for dependency in dependencies[task.id]))


def blocked_task_ids(plan: TaskPlan) -> tuple[str, ...]:
    tasks = ordered_tasks(plan)
    ready = set(ready_task_ids(plan))
    return tuple(task.id for task in tasks if task.status not in _TERMINAL_STATUSES and task.status != "in_progress" and task.id not in ready)


def topological_levels(plan: TaskPlan) -> tuple[tuple[str, ...], ...]:
    tasks = ordered_tasks(plan)
    dependencies = effective_dependencies(plan)
    completed: set[str] = set()
    remaining = [task.id for task in tasks]
    levels: list[tuple[str, ...]] = []

    while remaining:
        current = tuple(task_id for task_id in remaining if all(dependency in completed for dependency in dependencies[task_id]))
        if not current:
            raise TaskPlanError("task plan has a cycle and cannot be topologically sorted")
        levels.append(current)
        completed.update(current)
        remaining = [task_id for task_id in remaining if task_id not in completed]
    return tuple(levels)


def project_plan(plan: TaskPlan) -> dict[str, object]:
    return {
        "mode": plan.mode,
        "revision": plan.revision,
        "ready_task_ids": list(ready_task_ids(plan)),
        "blocked_task_ids": list(blocked_task_ids(plan)),
        "topological_levels": [list(level) for level in topological_levels(plan)],
        "tasks": [task.to_dict() for task in plan.tasks],
    }
