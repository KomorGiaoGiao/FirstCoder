"""session resume 编排入口。

resume 的底层事实仍来自完整 append-only event log；checkpoint 只影响下一轮
provider context 投影，不是 resume 存储边界。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog, require_usable_record
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
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def resume(self, session_id: str) -> ResumeResult:
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = require_usable_record(catalog.get_session(session_id))

        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            data_root=self.data_root,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        session = bootstrap.resume(session_id)
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=record)
