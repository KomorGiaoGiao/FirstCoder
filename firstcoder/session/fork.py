"""Fork existing sessions into new editable sessions."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, new_session_id
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError, SessionNotFoundError
from firstcoder.session.models import ResumeResult
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class ForkSessionService:
    """Copy a session event log and resume the copy."""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    sandbox_access: SandboxAccess | None = None
    catalog: SessionCatalog | None = None

    def fork(self, source_session_id: str, *, title: str | None = None) -> ResumeResult:
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = catalog.get_session(source_session_id)
        if record.status == "corrupt":
            raise SessionCorruptError(record.error or f"session is corrupt: {source_session_id}")
        if record.status == "empty":
            raise SessionEmptyError(f"session is empty: {source_session_id}")

        events = self.store.list_events(source_session_id)
        if not events:
            raise SessionNotFoundError(f"session not found: {source_session_id}")

        forked_session_id = new_session_id()
        for event in events:
            self.store.append_event(_fork_event(event, source_session_id, forked_session_id))
        SessionEventWriter(store=self.store, session_id=forked_session_id).append_session_metadata_updated(
            forked_from=source_session_id,
            title=title or f"Fork of {record.title}",
        )
        self._copy_archives(source_session_id, forked_session_id)

        data_root = Path(self.data_root) if self.data_root is not None else self.store.root
        session = AgentSession.resume(
            store=self.store,
            session_id=forked_session_id,
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self._tools(),
            permission_manager=create_project_permission_manager(
                self.project_root,
                grants=FilePermissionGrantStore(data_root / "permissions.json"),
            ),
            sandbox_access=self.sandbox_access,
        )
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=catalog.get_session(forked_session_id))

    def _tools(self) -> list[Tool] | None:
        return self.tools_provider() if self.tools_provider is not None else self.tools

    def _copy_archives(self, source_session_id: str, forked_session_id: str) -> None:
        source = self.store.root / "archives" / source_session_id
        if not source.exists():
            return
        destination = self.store.root / "archives" / forked_session_id
        shutil.copytree(source, destination, dirs_exist_ok=True)


def _fork_event(event: SessionEvent, source_session_id: str, forked_session_id: str) -> SessionEvent:
    payload = _rewrite_session_id(event.payload, source_session_id, forked_session_id)
    return SessionEvent(
        id=new_event_id(),
        session_id=forked_session_id,
        type=event.type,
        payload=payload,
        created_at=event.created_at,
    )


def _rewrite_session_id(value, source_session_id: str, forked_session_id: str):
    if isinstance(value, dict):
        return {key: _rewrite_session_id(item, source_session_id, forked_session_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_session_id(item, source_session_id, forked_session_id) for item in value]
    if value == source_session_id:
        return forked_session_id
    return value
