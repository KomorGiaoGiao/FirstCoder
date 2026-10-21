"""new / resume / fork / factory 共用的 AgentSession 组装入口。

窄入口: 只有这里知道怎么把 AGENTS.md, 技能目录, 权限管理器和沙箱组装成一个
绑定项目的 AgentSession, app/UI 层不需要关心这些初始化细节。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.context.identity import new_session_id
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class SessionBootstrap:
    """唯一知道如何组装项目级 AgentSession 的地方。"""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None

    def resolved_data_root(self) -> Path:
        return Path(self.data_root) if self.data_root is not None else self.store.root

    def resolve_tools(self) -> list[Tool] | None:
        return self.tools_provider() if self.tools_provider is not None else self.tools

    def permission_manager(self) -> PermissionManager:
        return create_project_permission_manager(
            self.project_root,
            grants=FilePermissionGrantStore(self.resolved_data_root() / "permissions.json"),
        )

    def create(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.create(
            store=self.store,
            session_id=session_id or new_session_id(),
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
        )

    def resume(self, session_id: str) -> AgentSession:
        # 与 create 共用同一套组装参数, 差别只在 AgentSession.resume 走事件重放
        return AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
        )

    def from_project(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession.from_project(
            store=self.store,
            session_id=session_id or new_session_id(),
            project_root=self.project_root,
            tools=self.resolve_tools(),
            permission_manager=self.permission_manager(),
            sandbox_access=self.sandbox_access,
        )
