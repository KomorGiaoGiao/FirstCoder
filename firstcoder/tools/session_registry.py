"""会话级工具注册表工厂。"""

from __future__ import annotations

from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.types import Tool


def create_session_tool_registry(
    *,
    session_id: str,
    runtime_state: SessionRuntimeState | None = None,
    tools: list[Tool] | None = None,
) -> ToolRegistry:
    """创建单个会话专用的工具注册表。

    `task_boundary` 依赖当前会话的 `SessionRuntimeState`，不能放进无状态默认工具集。
    这个工厂集中处理会话级注入，后续权限、确认策略也可以在这里包一层。
    """

    state = runtime_state or SessionRuntimeState(session_id=session_id)
    registry = ToolRegistry(tools or [])
    if "task_boundary" not in registry.names():
        registry.register(create_task_boundary_tool(state))
    return registry

