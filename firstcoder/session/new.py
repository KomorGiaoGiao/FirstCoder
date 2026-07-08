"""Create fresh interactive sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from firstcoder.agent.prompt_inputs import read_agents_md
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.context.identity import new_session_id
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.models import ResumeResult
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


@dataclass(slots=True)
class NewSessionService:
    """Create a new session and return its runtime object."""

    store: JsonlSessionStore
    project_root: str | Path
    data_root: str | Path | None = None
    tools: list[Tool] | None = None
    sandbox_access: SandboxAccess | None = None

    def create(self, *, title: str | None = None) -> ResumeResult:
        data_root = Path(self.data_root) if self.data_root is not None else self.store.root
        session_id = new_session_id()
        session = AgentSession.create(
            store=self.store,
            session_id=session_id,
            agents_md=read_agents_md(self.project_root),
            skill_catalog=discover_all_skills(self.project_root),
            tools=self.tools,
            permission_manager=create_project_permission_manager(
                self.project_root,
                grants=FilePermissionGrantStore(data_root / "permissions.json"),
            ),
            sandbox_access=self.sandbox_access,
        )
        if title:
            SessionEventWriter(store=self.store, session_id=session_id).append_session_metadata_updated(title=title)
        record = SessionCatalog(self.store.root).get_session(session_id)
        return ResumeResult(session=session, record=record)
