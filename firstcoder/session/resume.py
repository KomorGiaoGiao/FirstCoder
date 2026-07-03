"""session resume 编排入口。

resume 的底层事实仍来自完整 append-only event log；checkpoint 只影响下一轮
provider context 投影，不是 resume 存储边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError
from firstcoder.session.models import ResumeResult
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class ResumeService:
    """把用户可见 resume 入口封装成窄服务。"""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def resume(self, session_id: str) -> ResumeResult:
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = catalog.get_session(session_id)
        if record.status == "corrupt":
            raise SessionCorruptError(record.error or f"session is corrupt: {session_id}")
        if record.status == "empty":
            raise SessionEmptyError(f"session is empty: {session_id}")

        data_root = Path(self.data_root) if self.data_root is not None else self.store.root
        session = AgentSession.resume(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            tools=self.tools,
            permission_manager=create_project_permission_manager(
                self.project_root,
                grants=FilePermissionGrantStore(data_root / "permissions.json"),
            ),
            sandbox_access=self.sandbox_access,
        )
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=record)
