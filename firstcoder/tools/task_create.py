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
            description="Create or append tasks by stable task ID without replacing existing work.",
            parameters=parameters,
        ),
        executor=task_create,
    )
