"""Dedicated task-content revision tool."""

from __future__ import annotations

from firstcoder.planning.service import TaskPlanService
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.task_plan_support import execute_task_plan_mutation
from firstcoder.tools.types import Tool
from firstcoder.utils.schema import object_schema


def create_task_revise_tool(service: TaskPlanService) -> Tool:
    def task_revise(*, expected_revision: int, revisions: object):
        return execute_task_plan_mutation(
            "task_revise",
            lambda: service.revise(expected_revision=expected_revision, revisions=revisions),
        )

    parameters = object_schema(
        {
            "expected_revision": {"type": "integer", "minimum": 0},
            "revisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["id", "content"],
                    "additionalProperties": False,
                },
            },
        },
        required=["expected_revision", "revisions"],
    )
    parameters["additionalProperties"] = False
    return Tool(
        definition=ToolDefinition(
            name="task_revise",
            description="Revise task wording by stable ID; do not use it for progress updates.",
            parameters=parameters,
        ),
        executor=task_revise,
    )
