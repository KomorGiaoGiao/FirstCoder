"""会话级工具注册表工厂。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Collection, Protocol

from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.task_boundary import TaskBoundaryPolicy, TaskBoundaryService
from firstcoder.permissions.manager import PermissionManager
from firstcoder.tools.permission_registry import PermissionAwareToolRegistry
from firstcoder.tools.retrieve_archive import create_retrieve_archive_tool
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.types import Tool


class ToolRegistryLike(Protocol):
    """AgentSession 需要的工具注册表最小接口。"""

    def register(self, tool: Tool) -> None:
        ...

    def definitions(self):
        ...

    def names(self) -> list[str]:
        ...

    def tools(self) -> list[Tool]:
        ...

    def execute(self, name: str, arguments=None):
        ...


def create_session_tool_registry(
    *,
    session_id: str,
    runtime_state: SessionRuntimeState | None = None,
    tools: list[Tool] | None = None,
    known_message_ids: Collection[str] | None = None,
    single_observation_basis_message_ids: Collection[str] = (),
    task_boundary_required_stable_count: int = 2,
    permission_manager: PermissionManager | None = None,
    archive_root: str | Path | None = None,
    current_turn: Callable[[], int] | None = None,
) -> ToolRegistryLike:
    """创建单个会话专用的工具注册表。

    `task_boundary` 依赖当前会话的 `SessionRuntimeState`，不能放进无状态默认工具集。
    这个工厂集中处理会话级注入，后续权限、确认策略也可以在这里包一层。
    """

    state = runtime_state or SessionRuntimeState(session_id=session_id)
    boundary_service = TaskBoundaryService(
        required_stable_count=task_boundary_required_stable_count,
        known_message_ids=known_message_ids,
        policy=TaskBoundaryPolicy(single_observation_basis_message_ids=single_observation_basis_message_ids),
    )
    supplied_tools = tools or []
    if any(tool.name == "retrieve_archive" for tool in supplied_tools):
        raise ValueError("retrieve_archive is a reserved session-scoped tool name")
    registry = ToolRegistry(supplied_tools)
    if "task_boundary" not in registry.names():
        registry.register(create_task_boundary_tool(state, service=boundary_service))
    if archive_root is not None:
        registry.register(
            create_retrieve_archive_tool(
                session_id=session_id,
                archive_root=archive_root,
                current_turn=current_turn or (lambda: 0),
            )
        )
    if permission_manager is not None:
        return PermissionAwareToolRegistry(registry, permission_manager)
    return registry
