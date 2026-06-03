"""TUI session slash command 处理。

这一层只把 `/sessions`、`/session`、`/resume`、`/share`、`/rename` 映射到
session 层服务；Textual widget 不直接扫描 JSONL，也不直接导出 Markdown。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionError
from firstcoder.session.models import SessionRecord, ShareOptions
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService


class SessionRuntimeLike(Protocol):
    session_id: str


@dataclass(slots=True)
class SessionCommandHandler:
    """处理用户可见 session 命令。"""

    catalog: SessionCatalog
    current_session: SessionRuntimeLike | None = None
    resume_service: ResumeService | None = None
    share_service: SessionShareService | None = None
    store: JsonlSessionStore | None = None
    on_resume: Callable[[SessionRuntimeLike], None] | None = None

    def handle(self, text: str) -> CommandResult:
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        parts = command.split()
        name = parts[0]
        args = parts[1:]

        try:
            if name == "/sessions":
                return CommandResult(handled=True, output=self._list_sessions())
            if name == "/session":
                return CommandResult(handled=True, output=self._show_session(args))
            if name == "/resume":
                return CommandResult(handled=True, output=self._resume(args))
            if name == "/share":
                return CommandResult(handled=True, output=self._share(args))
            if name == "/rename":
                return CommandResult(handled=True, output=self._rename(args))
        except SessionError as exc:
            return CommandResult(handled=True, output=f"Session error: {exc}")

        return CommandResult(handled=False)

    def _list_sessions(self) -> str:
        records = self.catalog.list_sessions()
        if not records:
            return "No sessions."
        lines = ["Sessions:"]
        for record in records:
            lines.append(
                "- "
                f"{record.session_id} "
                f"{record.title} "
                f"updated={_value(record.updated_at)} "
                f"messages={record.message_count} "
                f"status={record.status}"
            )
        return "\n".join(lines)

    def _show_session(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /session <session_id>"
        return _render_session_record(self.catalog.get_session(args[0]))

    def _resume(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /resume <session_id>"
        if self.resume_service is None:
            return "Resume unavailable: resume service is not configured"

        result = self.resume_service.resume(args[0])
        self.current_session = result.session
        if self.on_resume is not None:
            self.on_resume(result.session)
        return f"Resumed session: {result.record.session_id} {result.record.title}"

    def _share(self, args: list[str]) -> str:
        if self.share_service is None:
            return "Share unavailable: share service is not configured"

        include_tool_results = "--tool-results" in args
        session_args = [arg for arg in args if not arg.startswith("--")]
        if len(session_args) > 1:
            return "Usage: /share [session_id] [--tool-results]"
        session_id = session_args[0] if session_args else self._current_session_id()
        if session_id is None:
            return "Share unavailable: no current session"

        path = self.share_service.export_markdown(
            session_id,
            options=ShareOptions(include_tool_results=include_tool_results),
        )
        return f"Share exported: {Path(path)}"

    def _rename(self, args: list[str]) -> str:
        title = " ".join(args).strip()
        if not title:
            return "Usage: /rename <title>"
        session_id = self._current_session_id()
        if session_id is None:
            return "Rename unavailable: no current session"
        if self.store is None:
            return "Rename unavailable: session store is not configured"

        SessionEventWriter(store=self.store, session_id=session_id).append_session_metadata_updated(title=title)
        return f"Renamed session: {session_id} {title}"

    def _current_session_id(self) -> str | None:
        if self.current_session is None:
            return None
        return self.current_session.session_id


def _render_session_record(record: SessionRecord) -> str:
    return "\n".join(
        [
            f"Session: {record.session_id}",
            f"Title: {record.title}",
            f"Status: {record.status}",
            f"Created: {_value(record.created_at)}",
            f"Updated: {_value(record.updated_at)}",
            f"Workspace: {_value(record.workspace)}",
            f"Model: {_model_label(record.provider, record.model)}",
            f"Messages: {record.message_count}",
            f"User turns: {record.user_turn_count}",
            f"Checkpoints: {record.checkpoint_count}",
            f"Archives: {record.archive_count}",
            f"Latest user: {_value(record.latest_user_input)}",
            f"Latest assistant: {_value(record.latest_assistant_output)}",
        ]
    )


def _model_label(provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return provider or model or "-"


def _value(value: object | None) -> str:
    if value in (None, ""):
        return "-"
    return str(value)
