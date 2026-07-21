"""session resume 编排入口。

resume 的底层事实仍来自完整 append-only event log；checkpoint 只影响下一轮
provider context 投影，不是 resume 存储边界。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.versions import CONTEXT_EVENT_SCHEMA_VERSION
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog, require_usable_record
from firstcoder.session.catalog import is_safe_session_id
from firstcoder.session.errors import (
    SessionInvalidIdError,
    SessionNotFoundError,
    SessionUnsupportedSchemaError,
)
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
        validate_session_schema(self.store, session_id)
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


def validate_session_schema(store: JsonlSessionStore, session_id: str) -> None:
    """Reject event logs that cannot be replayed by the current runtime."""

    if not is_safe_session_id(session_id):
        raise SessionInvalidIdError(f"invalid session_id: {session_id!r}")
    path = store.sessions_dir / f"{session_id}.jsonl"
    if not path.exists():
        raise SessionNotFoundError(f"session not found: {session_id}")

    actual = None
    saw_valid_envelope = False
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                if not saw_valid_envelope:
                    return
                break
            if not isinstance(envelope, dict) or envelope.get("type") != "session_created":
                saw_valid_envelope = isinstance(envelope, dict)
                continue
            saw_valid_envelope = True
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                actual = payload.get("context_event_schema_version")
            break
    if not saw_valid_envelope:
        return
    actual_version = str(actual) if actual is not None else "missing"
    if actual_version != CONTEXT_EVENT_SCHEMA_VERSION:
        raise SessionUnsupportedSchemaError(
            session_id=session_id,
            actual_version=actual_version,
            expected_version=CONTEXT_EVENT_SCHEMA_VERSION,
        )
