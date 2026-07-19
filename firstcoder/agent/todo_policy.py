"""当前任务的 Todo 状态读取与结束前一次性对账。"""

from __future__ import annotations

from firstcoder.agent.session import AgentSession


class TodoPolicy:
    """从 SessionView 读取当前任务的 Todo，不主动打断工具循环。"""

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def final_reconciliation_instruction(self) -> str | None:
        unfinished = self.latest_unfinished_todos()
        if not unfinished:
            return None
        lines = [
            "Before finalizing, reconcile the unfinished Todo items.",
            "Continue required work, update completed or cancelled statuses, "
            "or explain the real blocker. Do not claim completion while required work remains.",
        ]
        lines.extend(
            f"- [{item.get('status', 'pending')}] {item.get('content', '')}"
            for item in unfinished
        )
        return "\n".join(lines)

    def latest_unfinished_todos(self) -> list[dict[str, object]]:
        view = self.session.rebuild_view()
        if not self._view_has_active_task_todos(view):
            return []
        return [
            item
            for item in view.todos
            if isinstance(item, dict) and item.get("status") in {"pending", "in_progress"}
        ]

    def _view_has_active_task_todos(self, view) -> bool:
        if not view.todo_initialized:
            return False
        active_task_hash = self.session.runtime_state.active_task_hash
        if view.todo_task_hash is not None and active_task_hash is not None:
            return view.todo_task_hash == active_task_hash
        return True
