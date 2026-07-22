"""Shared model-facing formatting for task-plan mutation tools."""

from __future__ import annotations

from collections.abc import Callable

from firstcoder.planning.models import TaskPlan
from firstcoder.planning.projection import ordered_tasks
from firstcoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from firstcoder.planning.service import TaskPlanMutation
from firstcoder.tools.types import ToolResult, make_error_result, make_text_result


def format_task_plan_snapshot(plan: TaskPlan, projection: dict[str, object]) -> str:
    """Render the concise authoritative task state that the model must see."""

    lines = [f"Task plan revision {plan.revision} ({plan.mode})", ""]
    for task in ordered_tasks(plan):
        details: list[str] = []
        if task.owner:
            details.append(f"owner={task.owner}")
        if task.depends_on:
            details.append(f"depends_on={','.join(task.depends_on)}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {task.id} [{task.status}]: {task.content}{suffix}")

    ready = projection.get("ready_task_ids", [])
    blocked = projection.get("blocked_task_ids", [])
    lines.extend(
        [
            "",
            f"Ready task IDs: {_format_task_ids(ready)}",
            f"Blocked task IDs: {_format_task_ids(blocked)}",
        ]
    )
    return "\n".join(lines)


def _format_task_ids(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(task_id) for task_id in value)


def execute_task_plan_mutation(
    tool_name: str,
    mutate: Callable[[], TaskPlanMutation],
    *,
    command_error_message: Callable[[TaskPlanCommandError], str] | None = None,
) -> ToolResult:
    try:
        mutation = mutate()
    except TaskPlanRevisionConflict as error:
        return make_error_result(
            tool_name,
            f"Revision conflict: expected {error.expected}, actual {error.actual}. " "Call task_list, then retry with the latest revision.",
            expected_revision=error.expected,
            actual_revision=error.actual,
        )
    except TaskPlanCommandError as error:
        message = command_error_message(error) if command_error_message is not None else str(error)
        return make_error_result(tool_name, message)

    return make_text_result(
        tool_name,
        f"Task plan revision {mutation.plan.revision}",
        revision=mutation.plan.revision,
        changed=mutation.changed,
        changes=[dict(change) for change in mutation.changes],
        snapshot=mutation.plan.to_dict(),
        projection=mutation.projection,
    )
