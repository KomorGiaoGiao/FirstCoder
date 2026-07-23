"""Unified creation and final-reconciliation prompts for task plans."""

from __future__ import annotations

from firstcoder.agent.session import AgentSession
from firstcoder.planning.models import TaskPlan
from firstcoder.planning.projection import ordered_tasks, project_plan

_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})


def render_current_task_plan_snapshot(plan: TaskPlan) -> str:
    """Render the latest plan as ephemeral provider context."""

    projection = project_plan(plan)
    lines = [
        "Current TaskPlan snapshot (authoritative for this request):",
        f"revision={plan.revision} mode={plan.mode}",
    ]
    for task in ordered_tasks(plan):
        details: list[str] = []
        if task.owner:
            details.append(f"owner={task.owner}")
        if task.depends_on:
            details.append(f"depends_on={','.join(task.depends_on)}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {task.id} [{task.status}]: {task.content}{suffix}")
    lines.extend(
        [
            f"ready={_format_task_ids(projection['ready_task_ids'])}",
            f"blocked={_format_task_ids(projection['blocked_task_ids'])}",
        ]
    )
    return "\n".join(lines)


def _format_task_ids(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ",".join(str(task_id) for task_id in value)


class TaskPlanPolicy:
    """Read the persisted task plan and return optional loop instructions."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def final_reconciliation_instruction(self) -> str | None:
        plan = self.session.rebuild_view().task_plan
        if plan is None:
            return None

        projection = project_plan(plan)
        unfinished = [task for task in ordered_tasks(plan) if task.status not in _TERMINAL_STATUSES]
        if not unfinished:
            return None

        lines = [
            f"Before finalizing, reconcile the unfinished {projection['mode']} task plan.",
            "Use task_update by task ID to update statuses locally; " "do not recreate or rebuild the plan just to report progress.",
            "Continue required work, or explain the real blocker. " "Do not claim completion while required tasks remain unfinished.",
        ]
        lines.extend(f"- [{task.status}] {task.id}: {task.content}" for task in unfinished)
        return "\n".join(lines)
