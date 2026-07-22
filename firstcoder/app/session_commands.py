"""TUI session slash command 处理。

这一层只把 `/sessions`、`/session`、`/resume`、`/share`、`/rename` 映射到
session 层服务；Textual widget 不直接扫描 JSONL，也不直接导出 Markdown。
"""

from __future__ import annotations

from firstcoder.utils.text import display_value, model_label

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.fork import ForkSessionService
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionError
from firstcoder.session.models import SessionRecord, ShareOptions
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService

SESSION_LIST_VISIBLE_LIMIT = 20


class SessionRuntimeLike(Protocol):
    session_id: str


@dataclass(slots=True)
class SessionCommandHandler:
    """处理用户可见 session 命令。"""

    catalog: SessionCatalog
    current_session: SessionRuntimeLike | None = None
    new_service: NewSessionService | None = None
    fork_service: ForkSessionService | None = None
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
            if name == "/new":
                output = self._new(args)
                return CommandResult(handled=True, output=output, action={"type": "new_session"})
            if name == "/fork":
                return CommandResult(handled=True, output=self._fork(args))
            if name == "/sessions":
                return CommandResult(handled=True, output=self._list_sessions())
            if name == "/session":
                return CommandResult(handled=True, output=self._show_session(args))
            if name == "/resume":
                return self._resume(args)
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
        visible = records[:SESSION_LIST_VISIBLE_LIMIT]
        lines = [_session_list_header(len(visible), len(records))]
        for record in visible:
            lines.append("- " f"{record.session_id} " f"{record.title} " f"updated={display_value(record.updated_at)} " f"messages={record.message_count} " f"status={record.status}")
        return "\n".join(lines)

    def _new(self, args: list[str]) -> str:
        if self.new_service is None:
            return "New session unavailable: new session service is not configured"
        title = " ".join(args).strip()
        result = self.new_service.create(title=title or None)
        self.current_session = result.session
        if self.on_resume is not None:
            self.on_resume(result.session)
        return f"New session: {result.record.session_id} {result.record.title}"

    def _fork(self, args: list[str]) -> str:
        if self.fork_service is None:
            return "Fork unavailable: fork service is not configured"
        source_session_id = self._current_session_id()
        if source_session_id is None:
            return "Fork unavailable: no current session"
        title = " ".join(args).strip()
        result = self.fork_service.fork(source_session_id, title=title or None)
        self.current_session = result.session
        if self.on_resume is not None:
            self.on_resume(result.session)
        return f"Forked session: {source_session_id} -> {result.record.session_id} {result.record.title}"

    def _show_session(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /session <session_id>"
        return _render_session_record(self.catalog.get_session(args[0]))

    def _resume(self, args: list[str]) -> CommandResult:
        if len(args) == 0:
            return self._resume_picker()
        if len(args) != 1:
            return CommandResult(handled=True, output="Usage: /resume [session_id]")
        if self.resume_service is None:
            return CommandResult(handled=True, output="Resume unavailable: resume service is not configured")

        result = self.resume_service.resume(args[0])
        self.current_session = result.session
        if self.on_resume is not None:
            self.on_resume(result.session)
        return CommandResult(
            handled=True,
            output=f"Resumed session: {result.record.session_id} {result.record.title}",
            action={"type": "replay_session", "session_id": result.record.session_id},
        )

    def _resume_picker(self) -> CommandResult:
        records = self.catalog.list_sessions()
        if not records:
            return CommandResult(handled=True, output="No sessions.")
        return CommandResult(
            handled=True,
            output=_render_resume_picker(records, selected_index=0),
            action={
                "type": "resume_picker",
                "selected_index": 0,
                "sessions": [
                    {
                        "session_id": record.session_id,
                        "title": record.title,
                        "message_count": record.message_count,
                        "status": record.status,
                    }
                    for record in records
                ],
            },
        )

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
            f"Created: {display_value(record.created_at)}",
            f"Updated: {display_value(record.updated_at)}",
            f"Workspace: {display_value(record.workspace)}",
            f"Model: {model_label(record.provider, record.model)}",
            f"Messages: {record.message_count}",
            f"User turns: {record.user_turn_count}",
            f"Checkpoints: {record.checkpoint_count}",
            f"Archives: {record.archive_count}",
            f"Latest user: {display_value(record.latest_user_input)}",
            f"Latest assistant: {display_value(record.latest_assistant_output)}",
        ]
    )


def _render_resume_picker(records: list[SessionRecord], *, selected_index: int) -> str:
    window_start, window_records = _visible_record_window(records, selected_index=selected_index)
    lines = [_session_picker_header(window_start, len(window_records), len(records))]
    for offset, record in enumerate(window_records):
        index = window_start + offset
        marker = ">" if index == selected_index else " "
        lines.append(f"{marker} {index + 1}. {record.session_id} {record.title} messages={record.message_count}")
    lines.append("Use up/down and enter to resume.")
    return "\n".join(lines)


def _visible_record_window(records: list[SessionRecord], *, selected_index: int, limit: int = SESSION_LIST_VISIBLE_LIMIT) -> tuple[int, list[SessionRecord]]:
    if not records:
        return 0, []
    selected_index = max(0, min(selected_index, len(records) - 1))
    limit = max(1, limit)
    if len(records) <= limit:
        return 0, records
    window_start = min(max(0, selected_index - limit + 1), len(records) - limit)
    return window_start, records[window_start : window_start + limit]


def _session_list_header(visible_count: int, total_count: int) -> str:
    if total_count <= visible_count:
        return "Sessions:"
    return f"Sessions: Showing {visible_count} of {total_count} sessions. Use /resume to browse all."


def _session_picker_header(window_start: int, visible_count: int, total_count: int) -> str:
    if total_count <= visible_count:
        return "Select a session:"
    window_end = window_start + visible_count
    return f"Select a session: Showing {window_start + 1}-{window_end} of {total_count} sessions"
