"""Unified creation and final-reconciliation prompts for task plans."""

from __future__ import annotations

from firstcoder.agent.session import AgentSession
from firstcoder.planning.projection import project_plan


_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})


class TaskPlanPolicy:
    """Read the persisted task plan and return optional loop instructions."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def creation_instruction(self, *, needs_plan: bool) -> str | None:
        plan = self.session.rebuild_view().task_plan
        if not needs_plan or plan is not None:
            return None
        return (
            "This is a complex task and no task plan exists yet. "
            "Create one with task_create before continuing."
        )

    def final_reconciliation_instruction(self) -> str | None:
        plan = self.session.rebuild_view().task_plan
        if plan is None:
            return None

        projection = project_plan(plan)
        unfinished = [
            task
            for task in projection["tasks"]
            if task["status"] not in _TERMINAL_STATUSES
        ]
        if not unfinished:
            return None

        lines = [
            f"Before finalizing, reconcile the unfinished {projection['mode']} task plan.",
            "Use task_update by task ID to update statuses locally; "
            "do not recreate or rebuild the plan just to report progress.",
        ]
        lines.extend(
            f"- [{task['status']}] {task['id']}: {task['content']}"
            for task in unfinished
        )
        return "\n".join(lines)
