"""session resume 编排入口。

resume 的底层事实仍来自完整 append-only event log；checkpoint 只影响下一轮
provider context 投影，不是 resume 存储边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError
from firstcoder.session.models import ResumeResult
from firstcoder.tools.types import Tool


@dataclass(slots=True)
class ResumeService:
    """把用户可见 resume 入口封装成窄服务。"""

    store: JsonlSessionStore
    project_root: str | Path
    tools: list[Tool] | None = None
    catalog: SessionCatalog | None = None

    def resume(self, session_id: str) -> ResumeResult:
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = catalog.get_session(session_id)
        if record.status == "corrupt":
            raise SessionCorruptError(record.error or f"session is corrupt: {session_id}")
        if record.status == "empty":
            raise SessionEmptyError(f"session is empty: {session_id}")

        session = AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            tools=self.tools,
            permission_manager=PermissionManager(policy=DefaultPermissionPolicy(self.project_root)),
        )
        return ResumeResult(session=session, record=record)
