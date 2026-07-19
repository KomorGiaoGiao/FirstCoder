"""Fork existing sessions into new editable sessions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, new_session_id
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog, require_usable_record
from firstcoder.session.errors import SessionNotFoundError
from firstcoder.session.models import ResumeResult
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
        record = require_usable_record(catalog.get_session(source_session_id))

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

        bootstrap = SessionBootstrap(
            store=self.store,
            project_root=self.project_root,
            data_root=self.data_root,
            tools=self.tools,
            tools_provider=self.tools_provider,
            sandbox_access=self.sandbox_access,
        )
        session = bootstrap.resume(forked_session_id)
        session.restore_pending_permission_execution()
        return ResumeResult(session=session, record=catalog.get_session(forked_session_id))

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
