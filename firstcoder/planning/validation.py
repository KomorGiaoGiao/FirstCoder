"""Structural and execution-state validation for canonical task plans."""

from __future__ import annotations

from firstcoder.planning.models import TaskPlan, TaskPlanError
from firstcoder.planning.projection import effective_dependencies, ordered_tasks, topological_levels


def validate_plan(plan: TaskPlan) -> None:
    tasks = ordered_tasks(plan)
    task_by_id = {task.id: task for task in tasks}
    if len(task_by_id) != len(tasks):
        raise TaskPlanError("task ids must be unique")

    for task in tasks:
        seen: set[str] = set()
        for dependency in task.depends_on:
            if dependency == task.id:
                raise TaskPlanError(f"task {task.id} cannot depend on itself")
            if dependency not in task_by_id:
                raise TaskPlanError(f"task {task.id} depends on missing task {dependency}")
            if dependency in seen:
                raise TaskPlanError(f"task {task.id} repeats dependency {dependency}")
            seen.add(dependency)

    topological_levels(plan)

    in_progress = [task for task in tasks if task.status == "in_progress"]
    if plan.mode == "linear" and len(in_progress) > 1:
        raise TaskPlanError("linear task plans allow at most one task in_progress")

    dependencies = effective_dependencies(plan)
    for task in in_progress:
        if not all(task_by_id[dependency].status == "completed" for dependency in dependencies[task.id]):
            raise TaskPlanError(f"task {task.id} is not ready to enter in_progress")
