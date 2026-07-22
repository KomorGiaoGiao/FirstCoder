"""Incremental task-plan creation tool."""

from __future__ import annotations

from firstcoder.planning.service import TaskPlanService
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.task_plan_support import execute_task_plan_mutation
from firstcoder.tools.types import Tool
from firstcoder.utils.schema import object_schema


def create_task_create_tool(service: TaskPlanService) -> Tool:
    def task_create(*, mode: str, expected_revision: int, tasks: object):
        return execute_task_plan_mutation(
            "task_create",
            lambda: service.create(
                mode=mode,
                expected_revision=expected_revision,
                tasks=tasks,
            ),
            command_error_message=lambda error: _create_error_message(service, error),
        )

    parameters = object_schema(
        {
            "mode": {"type": "string", "enum": ["linear", "dag"]},
            "expected_revision": {"type": "integer", "minimum": 0},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                        },
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "owner": {"type": ["string", "null"]},
                    },
                    "required": ["id", "content"],
                    "additionalProperties": False,
                },
            },
        },
        required=["mode", "expected_revision", "tasks"],
    )
    parameters["additionalProperties"] = False
    return Tool(
        definition=ToolDefinition(
            name="task_create",
            description=(
                "Create only genuinely new tasks by stable task ID. Call task_list first to avoid duplicates. "
                "Use task_update for status, owner, or dependency changes and task_revise for wording changes. "
                "Do not recreate existing tasks with new IDs."
            ),
            parameters=parameters,
        ),
        executor=task_create,
    )


def _create_error_message(service: TaskPlanService, error: Exception) -> str:
    plan = service.current()
    if plan is None:
        return str(error)

    in_progress_ids = [task.id for task in plan.tasks if task.status == "in_progress"]
    in_progress = ", ".join(in_progress_ids) if in_progress_ids else "none"
    return (
        f"{error}. Existing in-progress task IDs: {in_progress}. "
        "Call task_list to inspect existing task IDs. Use task_update or task_revise for existing tasks; "
        "use task_create only for genuinely new work."
    )
