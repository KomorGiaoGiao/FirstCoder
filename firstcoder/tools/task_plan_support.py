"""Shared model-facing formatting for task-plan mutation tools."""

from __future__ import annotations

from collections.abc import Callable

from firstcoder.planning.reducer import TaskPlanCommandError, TaskPlanRevisionConflict
from firstcoder.planning.service import TaskPlanMutation
from firstcoder.tools.types import ToolResult, make_error_result, make_text_result


def execute_task_plan_mutation(
    tool_name: str,
    mutate: Callable[[], TaskPlanMutation],
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
        return make_error_result(tool_name, str(error))

    return make_text_result(
        tool_name,
        f"Task plan revision {mutation.plan.revision}",
        revision=mutation.plan.revision,
        changed=mutation.changed,
        changes=[dict(change) for change in mutation.changes],
        snapshot=mutation.plan.to_dict(),
        projection=mutation.projection,
    )
