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
from firstcoder.agent.session import AgentSession
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
    on_resume: Callable[[AgentSession], None] | None = None

    def handle(self, text: str) -> CommandResult:
        """按首个词精确匹配子命令: /new /fork /sessions /session /resume /share /rename。

        SessionError(会话不存在、参数非法等) 统一兜底成一条错误输出并返回
        handled=True, 不再让后续 handler 尝试; 全部不匹配时 handled=False 让位。
        """
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        parts = command.split()
        name = parts[0]
        args = parts[1:]

        try:
            if name == "/new":
                output = self._new(args)
                # new_session action 让 TUI 清空输出区开始新会话
                return CommandResult(handled=True, output=output, action={"type": "new_session"})
            if name == "/fork":
                return CommandResult(handled=True, output=self._fork(args))
            if name == "/sessions":
                return CommandResult(handled=True, output=self._list_sessions())
            if name == "/session":
                return CommandResult(handled=True, output=self._show_session(args))
            if name == "/resume":
                # 裸 /resume 发 resume_picker action, 带 id 则恢复并发 replay_session
                return self._resume(args)
            if name == "/share":
                return CommandResult(handled=True, output=self._share(args))
            if name == "/rename":
                return CommandResult(handled=True, output=self._rename(args))
        except SessionError as exc:
            return CommandResult(handled=True, output=f"Session error: {exc}")

        return CommandResult(handled=False)

    def _list_sessions(self) -> str:
        """列出会话摘要, 最多展示前 SESSION_LIST_VISIBLE_LIMIT 条。

        排序由 catalog 负责(按更新时间倒序); 超出条数时头部提示用 /resume
        打开选择器浏览全部。返回直接展示的文本。
        """
        records = self.catalog.list_sessions()
        if not records:
            return "No sessions."
        visible = records[:SESSION_LIST_VISIBLE_LIMIT]
        lines = [_session_list_header(len(visible), len(records))]
        for record in visible:
            lines.append("- " f"{record.session_id} " f"{record.title} " f"updated={display_value(record.updated_at)} " f"messages={record.message_count} " f"status={record.status}")
        return "\n".join(lines)

    def _new(self, args: list[str]) -> str:
        """创建新会话并把当前会话切过去; args 拼成可选标题。

        两层切换: self.current_session 只更新本 handler 的引用, on_resume 回调
        (注入的是 CurrentSessionState.set_session) 才让所有命令与聊天面向新会话。
        配套的 new_session action 由 handle 附加, 通知 TUI 清空输出区。
        服务未配置时返回提示文本而不抛错, 保持命令层对可选依赖的容忍。
        """
        if self.new_service is None:
            return "New session unavailable: new session service is not configured"
        title = " ".join(args).strip()
        result = self.new_service.create(title=title or None)
        self.current_session = result.session
        if self.on_resume is not None:
            self.on_resume(result.session)
        return f"New session: {result.record.session_id} {result.record.title}"

    def _fork(self, args: list[str]) -> str:
        """从当前会话分叉出一个副本并切过去; 无当前会话时返回提示文本。

        会话切换的 on_resume 流向与 _new 相同。fork 内部对源会话的校验失败会
        抛 SessionError, 由 handle 统一兜底为 "Session error: ..." 输出。
        """
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
        """只读展示单个会话的摘要(消息数/checkpoint/最近输入输出等)。

        catalog.get_session 对未知 id 抛 SessionError, 由 handle 兜底成错误输出;
        参数个数不对时直接返回用法提示。
        """
        if len(args) != 1:
            return "Usage: /session <session_id>"
        return _render_session_record(self.catalog.get_session(args[0]))

    def _resume(self, args: list[str]) -> CommandResult:
        """恢复会话, 分裸参与带 id 两条路径。

        裸 /resume 不知道用户想恢复哪个, 发 resume_picker action 让 TUI 打开选择器
        交互选择; /resume <id> 目标明确, 直接恢复。恢复成功后附 replay_session
        action, TUI 收到后从 session view 重放渲染整段历史。

        on_resume 回调把新 session 交给 CurrentSessionState 代理, 之后所有命令
        与聊天都面向新会话。
        """
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
        """裸 /resume: 不直接恢复, 发 resume_picker action 让 TUI 打开选择器。

        output 是选择器的首帧静态渲染, 之后上下键移动由 TUI 侧原地重绘;
        action.sessions 是 picker item 的原始数据, TUI 经 session_picker_item
        工厂转成 TuiPickerItem。
        """
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
        """导出会话为可分享的 Markdown, 返回文件路径。

        默认不含工具结果(可能很长且含敏感输出), 需显式传 --tool-results。
        省略 session_id 时导出当前会话; 未知 id 的 SessionError 由 handle 兜底。
        """
        if self.share_service is None:
            return "Share unavailable: share service is not configured"

        include_tool_results = "--tool-results" in args
        # 分离 -- 标志与位置参数, 剩下最多允许一个 session_id
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
        """重命名当前会话; 空标题返回用法提示。

        改名是事件溯源式的: 往会话的 JSONL 事件日志追加一条
        session_metadata_updated 事件, catalog 读取时应用最新标题, 不改写历史记录。
        """
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
    """把 SessionRecord 渲染成多行摘要; display_value 把缺失字段显示为占位符。"""
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
    """渲染会话选择器的一帧: 贴底窗口 + ">" 选中标记; TUI 移动选择时原地重绘。"""
    window_start, window_records = _visible_record_window(records, selected_index=selected_index)
    lines = [_session_picker_header(window_start, len(window_records), len(records))]
    for offset, record in enumerate(window_records):
        index = window_start + offset
        marker = ">" if index == selected_index else " "
        lines.append(f"{marker} {index + 1}. {record.session_id} {record.title} messages={record.message_count}")
    lines.append("Use up/down and enter to resume.")
    return "\n".join(lines)


def _visible_record_window(records: list[SessionRecord], *, selected_index: int, limit: int = SESSION_LIST_VISIBLE_LIMIT) -> tuple[int, list[SessionRecord]]:
    """选中项贴底的滚动窗口: 选中项尽量停留在窗口最后一行, 向上才翻页。"""
    if not records:
        return 0, []
    selected_index = max(0, min(selected_index, len(records) - 1))
    limit = max(1, limit)
    if len(records) <= limit:
        return 0, records
    window_start = min(max(0, selected_index - limit + 1), len(records) - limit)
    return window_start, records[window_start : window_start + limit]


def _session_list_header(visible_count: int, total_count: int) -> str:
    """列表头部: 有截断时提示"展示 x/y, 用 /resume 浏览全部"。"""
    if total_count <= visible_count:
        return "Sessions:"
    return f"Sessions: Showing {visible_count} of {total_count} sessions. Use /resume to browse all."


def _session_picker_header(window_start: int, visible_count: int, total_count: int) -> str:
    """选择器头部: 有窗口截断时显示当前可见范围(1 起始)。"""
    if total_count <= visible_count:
        return "Select a session:"
    window_end = window_start + visible_count
    return f"Select a session: Showing {window_start + 1}-{window_end} of {total_count} sessions"
