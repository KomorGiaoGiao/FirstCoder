"""FirstCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天通过注入的 chat runner 处理，避免 Textual widget 直接依赖 provider/agent 细节。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Protocol

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Input, Markdown, Static

from firstcoder.app.commands import CommandResult
from firstcoder.app.tui_state import TuiEntryKind, TuiTodoItem, TuiTranscript, TuiTranscriptEntry


_HIDDEN_TOOL_STATUS_NAMES = {"task_boundary"}


def _observe_markdown_update(update_result) -> None:
    future = getattr(update_result, "_future", None)
    if future is None or not hasattr(future, "add_done_callback"):
        return

    def observe_cancelled_update(done_future) -> None:
        try:
            exception = done_future.exception()
        except asyncio.CancelledError:
            return
        if isinstance(exception, asyncio.CancelledError):
            return
        if exception is not None:
            raise exception

    future.add_done_callback(observe_cancelled_update)


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult:
        ...


class ChatRunnerLike(Protocol):
    def run_user_turn(self, content: str):
        ...


class CurrentSessionLike(Protocol):
    session_id: str


@dataclass(slots=True)
class FirstCoderTuiConfig:
    title: str = "FirstCoder"
    provider_name: str | None = None
    provider_model: str | None = None
    project_name: str | None = None


class FirstCoderApp(App[None]):
    """最小 TUI 外壳。"""

    CSS_PATH = "tui.tcss"
    BINDINGS = [("ctrl+c", "quit", "Quit")]
    STREAM_RENDER_INTERVAL_SECONDS = 0.03
    WORKING_ANIMATION_INTERVAL_SECONDS = 0.18
    WORKING_FRAMES = ("[.  ]", "[.. ]", "[...]", "[ ..]", "[  .]")

    def __init__(
        self,
        *,
        command_handler: CommandHandlerLike | None = None,
        chat_runner: ChatRunnerLike | None = None,
        current_session: CurrentSessionLike | None = None,
        config: FirstCoderTuiConfig | None = None,
    ) -> None:
        super().__init__()
        self.command_handler = command_handler
        self.chat_runner = chat_runner
        self.current_session = current_session
        self.config = config or FirstCoderTuiConfig()
        self._chat_busy = False
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry: TuiTranscriptEntry | None = None
        self._stream_rendered_text = ""
        self._stream_flush_timer: Timer | None = None
        self._reasoning_buffer = ""
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._working_timer: Timer | None = None
        self._live_tool_events_seen = False
        self._stream_segment_closed_for_tool = False
        self._activity_text = "idle · ready"
        self.transcript = TuiTranscript()

    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar", classes="topbar")
        with Vertical(id="main"):
            yield VerticalScroll(id="output")
            yield Static("", id="todo-panel", classes="todo-panel hidden")
            yield Static("idle · ready", id="activity", classes="activity-line")
            with Vertical(id="composer", classes="composer"):
                yield Input(placeholder="输入消息，或使用 /context、/compact status、/compact", id="input")

    def on_mount(self) -> None:
        self.title = self.config.title
        self._refresh_session_subtitle()
        self._write_line(
            "FirstCoder ready. Commands: /sessions, /session, /resume, /share, /rename, "
            "/context, /compact status, /compact",
            classes="message system-message",
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        self._write_line(f"> {text}", kind=TuiEntryKind.USER)

        if text.startswith("/"):
            if self.command_handler is None:
                self._write_line("Command handler is not configured.", kind=TuiEntryKind.ERROR)
                return

            result = self.command_handler.handle(text)
            if result.handled:
                self._write_line(result.output, kind=TuiEntryKind.COMMAND)
                self._refresh_session_subtitle()
                return
            self._write_line(f"Unknown command: {text}", kind=TuiEntryKind.ERROR)
            return

        if self.chat_runner is None:
            self._write_line("普通聊天入口尚未接入 AgentLoop。", kind=TuiEntryKind.ERROR)
            return

        if self._chat_busy:
            self._write_line(
                "Chat is still running. Please wait for the current turn to finish.",
                kind=TuiEntryKind.SYSTEM,
            )
            return

        pending = getattr(self.chat_runner, "last_pending_input", None)
        if getattr(pending, "kind", None) == "permission_confirmation":
            choice = _permission_choice_for_text(text, pending)
            if choice is None:
                self._write_line(_permission_options_text(pending), kind=TuiEntryKind.PERMISSION)
                return
            self._chat_busy = True
            self.run_worker(self._resume_permission_turn(pending.id, choice))
            return

        self._chat_busy = True
        self.run_worker(self._run_chat_turn(text))

    async def _resume_permission_turn(self, request_id: str, answer: str) -> None:
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler()
            previous_tool_handler = self._install_tool_event_handler()
            self._show_working_indicator("resuming with permission answer...")
            async_resume = getattr(self.chat_runner, "aresume_with_user_input", None)
            if async_resume is not None:
                response = await async_resume(request_id, answer)
                self._write_chat_response(response)
                return
            resume = getattr(self.chat_runner, "resume_with_user_input", None)
            if resume is None:
                self._write_line("Permission resume is not configured.", kind=TuiEntryKind.ERROR)
                return
            response = resume(request_id, answer)
        except Exception as exc:
            self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
            self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._chat_busy = False

        self._write_chat_response(response)

    async def _run_chat_turn(self, text: str) -> None:
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler()
            previous_tool_handler = self._install_tool_event_handler()
            self._show_working_indicator("planning next step...")
            async_runner = getattr(self.chat_runner, "arun_user_turn", None) if self.chat_runner else None
            if async_runner is not None:
                response = await async_runner(text)
            else:
                response = self.chat_runner.run_user_turn(text)
        except Exception as exc:
            self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
            self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._chat_busy = False

        self._write_chat_response(response)

    def _write_chat_response(self, response) -> None:
        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        content = getattr(response, "content", "")
        if self._stream_text_started:
            display_lines = [line for line in display_lines if _looks_like_tool_display_line(line)]
            self._flush_stream_text()
        if self._live_tool_events_seen:
            display_lines = [line for line in display_lines if not _looks_like_tool_display_line(line)]
        if display_lines:
            for line in display_lines:
                if line == content or _looks_like_markdown_response(line):
                    self._write_markdown_message(line)
                else:
                    self._write_line(line, kind=_display_line_kind(line), status=_display_line_status(line))
        elif not self._stream_text_started:
            self._write_markdown_message(content or "[assistant response has no text content]")
        self._write_pending_input()
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._set_activity("idle · ready")
        self._refresh_session_subtitle()

    def _refresh_session_subtitle(self) -> None:
        session_id = None
        if self.current_session is None:
            self.sub_title = ""
        else:
            session_id = self.current_session.session_id
            self.sub_title = f"Session: {session_id}"
        if getattr(self, "is_mounted", False):
            topbar = self.query_one("#topbar")
            if hasattr(topbar, "update"):
                topbar.update(self._topbar_text(session_id=session_id, width=self._topbar_width()))

    def _topbar_width(self) -> int | None:
        size = getattr(self, "size", None)
        width = getattr(size, "width", None)
        if isinstance(width, int) and width > 0:
            return max(1, width - 4)
        return None

    def _topbar_text(self, *, session_id: str | None = None, width: int | None = None) -> str:
        if session_id is None and self.current_session is not None:
            session_id = self.current_session.session_id
        brand = "[#7bba55]FirstCoder[/]"
        status = _activity_markup(self._activity_text)
        metadata_parts = [f"[#6e6d72]{escape(_short_session_id(session_id) if session_id else 'no session')}[/]"]
        if self.config.provider_name or self.config.provider_model:
            provider = escape(self.config.provider_name or "provider")
            model = escape(self.config.provider_model or "model")
            metadata_parts.append(f"[#6e6d72]{provider}/{model}[/]")
        mode = getattr(self.current_session, "mode", None) if self.current_session is not None else None
        if mode:
            mode_text = escape(str(mode))
            mode_color = "#b28443" if mode_text == "bypass" else "#6e6d72"
            metadata_parts.append(f"[{mode_color}]{mode_text}[/]")
        if self.config.project_name:
            metadata_parts.append(f"[#6e6d72]cwd {escape(self.config.project_name)}[/]")
        metadata = "   [#303238]·[/]   ".join(metadata_parts)
        compact = f"{brand}   [#303238]·[/]   {status}   [#303238]·[/]   {metadata}"
        if width is None:
            return compact
        content_width = _markup_width(brand) + _markup_width(status) + _markup_width(metadata)
        if width - content_width < 8:
            return compact
        left_gap = max(3, (width // 2) - _markup_width(brand) - (_markup_width(status) // 2))
        right_gap = width - _markup_width(brand) - left_gap - _markup_width(status) - _markup_width(metadata)
        if right_gap < 3:
            right_gap = 3
            left_gap = width - _markup_width(brand) - _markup_width(status) - _markup_width(metadata) - right_gap
        return f"{brand}{' ' * left_gap}{status}{' ' * right_gap}{metadata}"

    def _install_stream_event_handler(self):
        if self.chat_runner is None or not hasattr(self.chat_runner, "stream_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "stream_event_handler", None)
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._reasoning_buffer = ""
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._stop_working_animation()
        self._stream_segment_closed_for_tool = False

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            kind = getattr(event, "kind", None)
            text = getattr(event, "text", "") or ""
            if not text:
                return
            if kind == "reasoning_delta":
                self._stream_reasoning_started = True
                self._call_ui_thread(self._append_reasoning_text, text)
            elif kind == "text_delta":
                self._stream_text_started = True
                self._stream_text_needs_newline = True
                self._call_ui_thread(self._complete_working_indicator)
                self._call_ui_thread(self._append_stream_text, text)

        setattr(self.chat_runner, "stream_event_handler", handle_event)
        return previous_handler

    def _restore_stream_event_handler(self, previous_handler) -> None:
        if self.chat_runner is not None and hasattr(self.chat_runner, "stream_event_handler"):
            setattr(self.chat_runner, "stream_event_handler", previous_handler)

    def _install_tool_event_handler(self):
        if self.chat_runner is None or not hasattr(self.chat_runner, "tool_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "tool_event_handler", None)
        self._live_tool_events_seen = False

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            tool_call = getattr(event, "tool_call", None)
            tool_name = str(getattr(tool_call, "name", "") or "tool")
            if tool_name in _HIDDEN_TOOL_STATUS_NAMES:
                return
            line = _tool_status_text(event)
            if not line:
                return
            self._live_tool_events_seen = True
            self._call_ui_thread(self._close_stream_segment_for_tool)
            self._call_ui_thread(self._record_tool_activity, event)
            if tool_name == "todo" and str(getattr(event, "kind", "") or "") == "finished":
                self._call_ui_thread(self._refresh_todo_panel_from_tool_event, event)
            self._call_ui_thread(
                self._write_line,
                line,
                kind=_tool_event_entry_kind(event),
                label=_tool_event_label(event),
                status=_tool_event_status(event),
            )

        setattr(self.chat_runner, "tool_event_handler", handle_event)
        return previous_handler

    def _restore_tool_event_handler(self, previous_handler) -> None:
        if self.chat_runner is not None and hasattr(self.chat_runner, "tool_event_handler"):
            setattr(self.chat_runner, "tool_event_handler", previous_handler)

    def _call_ui_thread(self, callback, *args, **kwargs):
        if not getattr(self, "is_running", False):
            return callback(*args, **kwargs)
        if getattr(self, "_thread_id", None) == threading.get_ident():
            return callback(*args, **kwargs)
        return self.call_from_thread(callback, *args, **kwargs)

    def _write_line(
        self,
        text: str,
        *,
        classes: str | None = None,
        kind: TuiEntryKind = TuiEntryKind.SYSTEM,
        label: str | None = None,
        status: str | None = None,
    ) -> TuiTranscriptEntry:
        entry = self.transcript.add(kind, text, label=label, status=status)
        classes = classes or _entry_classes(entry)
        rendered = _entry_plain_text(entry)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            output.mount(Static(rendered, classes=classes))
            output.scroll_end(animate=False)
            return entry
        if hasattr(output, "write_line"):
            output.write_line(rendered)
        return entry

    def _record_tool_activity(self, event) -> None:
        tool_call = getattr(event, "tool_call", None)
        name = str(getattr(tool_call, "name", "") or "tool")
        status = _tool_event_status(event) or "unknown"
        summary = _tool_activity_summary(event)
        self.transcript.record_tool_activity(name, status, summary)
        if status == "success":
            self._show_working_indicator(_post_tool_reasoning_text(name))
            return
        self._stop_working_animation()
        self._set_activity(_activity_line_text(name, status))

    def _refresh_todo_panel_from_tool_event(self, event) -> None:
        tool_call = getattr(event, "tool_call", None)
        if str(getattr(tool_call, "name", "") or "") != "todo":
            return
        if str(getattr(event, "kind", "") or "") != "finished":
            return
        result = getattr(event, "result", None)
        if result is None or not getattr(result, "ok", False):
            return
        data = getattr(result, "data", {}) or {}
        todos = data.get("todos") if isinstance(data, dict) else None
        if not isinstance(todos, list):
            return
        self.transcript.update_todos([item for item in todos if isinstance(item, dict)])
        self._render_todo_panel()

    def _render_todo_panel(self) -> None:
        panel = self.query_one("#todo-panel")
        todos = self.transcript.todos
        if not todos:
            panel.update("")
            if hasattr(panel, "add_class"):
                panel.add_class("hidden")
            return
        if hasattr(panel, "remove_class"):
            panel.remove_class("hidden")
        panel.update(_todo_panel_text(todos))

    def _write_markdown_message(self, content: str, *, classes: str = "message assistant-message") -> None:
        entry = self.transcript.add(TuiEntryKind.ASSISTANT, content)
        text = _entry_markdown_text(entry)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            markdown = Markdown(classes=classes)
            output.mount(markdown)
            _observe_markdown_update(markdown.update(text))
            output.scroll_end(animate=False)
            return
        if hasattr(output, "write_line"):
            output.write_line(text)

    def _write_pending_input(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        if getattr(pending, "kind", None) == "permission_confirmation":
            self._write_line(_permission_prompt_text(pending), kind=TuiEntryKind.PERMISSION)
            self._set_activity("waiting · permission")
            return
        question = str(getattr(pending, "question", "") or "需要用户输入。")
        self._write_line(f"需要用户输入：\n{question}", kind=TuiEntryKind.PERMISSION)
        self._set_activity("waiting · input")

    def _append_stream_line(self, label: str, text: str, *, include_label: bool) -> None:
        output = self.query_one("#output")
        line = f"{label}: {text}" if include_label else text
        if hasattr(output, "mount"):
            entry = self.transcript.add(TuiEntryKind.REASONING, line)
            output.mount(Static(_entry_plain_text(entry), classes="message reasoning-message"))
            output.scroll_end(animate=False)
            return
        if hasattr(output, "write"):
            output.write(line)

    def _show_working_indicator(self, text: str) -> None:
        self._reasoning_buffer = text
        self._reasoning_is_fallback = True
        self._working_text = text
        self._working_frame_index = 0
        self._set_activity(self._working_indicator_body())
        self._start_working_animation()

    def _complete_working_indicator(self) -> None:
        self._stop_working_animation()
        self._set_activity("streaming · response")

    def _append_reasoning_text(self, text: str) -> None:
        if self._reasoning_is_fallback:
            self._reasoning_buffer = ""
            self._reasoning_is_fallback = False
            self._working_text = ""
        self._reasoning_buffer += text
        self._set_activity(self._working_indicator_body(self._reasoning_buffer))
        self._start_working_animation()

    def _working_indicator_body(self, text: str | None = None) -> str:
        frame = self.WORKING_FRAMES[self._working_frame_index % len(self.WORKING_FRAMES)]
        return f"thinking {frame} {text if text is not None else self._working_text}"

    def _start_working_animation(self) -> None:
        if self._working_timer is not None:
            return
        if getattr(self, "_loop", None) is None:
            return
        self._working_timer = self.set_interval(
            self.WORKING_ANIMATION_INTERVAL_SECONDS,
            self._advance_working_animation,
            name="working-indicator",
        )

    def _stop_working_animation(self) -> None:
        if self._working_timer is None:
            return
        self._working_timer.stop()
        self._working_timer = None

    def _advance_working_animation(self) -> None:
        self._working_frame_index += 1
        text = self._working_text or self._reasoning_buffer
        self._set_activity(self._working_indicator_body(text))

    def _set_activity(self, text: str) -> None:
        self._activity_text = text
        activity = self.query_one("#activity")
        if hasattr(activity, "update"):
            activity.update(text)
        if getattr(self, "is_mounted", False):
            topbar = self.query_one("#topbar")
            if hasattr(topbar, "update"):
                topbar.update(self._topbar_text(width=self._topbar_width()))

    def _append_stream_text(self, text: str) -> None:
        if self._stream_segment_closed_for_tool:
            self._start_new_stream_segment()
        self._stream_text_buffer += text
        if self._stream_text_entry is None:
            self._stream_text_entry = self.transcript.add(TuiEntryKind.ASSISTANT, self._stream_text_buffer)
        else:
            self._stream_text_entry.body = self._stream_text_buffer
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            if self._stream_text_widget is None:
                self._stream_text_widget = Markdown(classes="message assistant-message streaming")
                output.mount(self._stream_text_widget)
            if not self._stream_rendered_text:
                self._flush_stream_text()
            else:
                self._schedule_stream_flush()
            output.scroll_end(animate=False)
            return
        if hasattr(output, "write"):
            prefix = "FirstCoder:\n" if self._stream_text_buffer == text else ""
            output.write(f"{prefix}{text}")

    def _close_stream_segment_for_tool(self) -> None:
        if self._stream_text_widget is None and not self._stream_text_buffer:
            return
        self._flush_stream_text()
        self._stream_segment_closed_for_tool = True

    def _start_new_stream_segment(self) -> None:
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._stream_segment_closed_for_tool = False

    def _schedule_stream_flush(self) -> None:
        if self._stream_flush_timer is not None:
            return
        if getattr(self, "_loop", None) is None:
            return
        self._stream_flush_timer = self.set_timer(
            self.STREAM_RENDER_INTERVAL_SECONDS,
            self._flush_stream_text,
            name="stream-markdown-flush",
        )

    def _flush_stream_text(self) -> None:
        self._stream_flush_timer = None
        if self._stream_text_widget is None:
            return
        if self._stream_rendered_text == self._stream_text_buffer:
            return
        self._stream_rendered_text = self._stream_text_buffer
        _observe_markdown_update(self._stream_text_widget.update(f"FirstCoder:\n\n{self._stream_rendered_text}"))
        output = self.query_one("#output")
        if hasattr(output, "scroll_end"):
            output.scroll_end(animate=False)


def _permission_choice_for_text(text: str, pending) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    aliases = {
        "1": "deny",
        "no": "deny",
        "deny": "deny",
        "2": "allow_once",
        "allow_once": "allow_once",
        "once": "allow_once",
        "allow": "allow_once",
        "3": "allow_always_same_scope",
        "allow_always": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    if normalized in aliases:
        return aliases[normalized]
    for option in getattr(pending, "options", []) or []:
        if normalized in {str(option.id).lower(), str(option.label).strip().lower().replace(" ", "_")}:
            return str(option.id)
    return None


def _short_session_id(session_id: str) -> str:
    if len(session_id) <= 14:
        return session_id
    if session_id.startswith("sess_"):
        return session_id[:13]
    return session_id[:12]


def _markup_width(markup: str) -> int:
    return len(Text.from_markup(markup).plain)


def _activity_markup(text: str) -> str:
    color = "#7bba55"
    if text.startswith("waiting"):
        color = "#b28443"
    elif text.startswith("running"):
        color = "#808185"
    elif text.startswith("streaming"):
        color = "#6e6d72"
    elif text.startswith("error"):
        color = "#c85f5f"
    return f"[{color}]{escape(text)}[/]"


def _permission_options_text(pending) -> str:
    options = getattr(pending, "options", []) or []
    if not options:
        return "请回复权限选择：deny / allow_once / allow_always_same_scope"
    rendered = ", ".join(f"{option.id} ({option.label})" for option in options)
    return f"请回复权限选择：{rendered}"


def _permission_prompt_text(pending) -> str:
    payload = getattr(pending, "payload", {}) or {}
    action = str(payload.get("action") or "")
    target = str(payload.get("target") or "")
    reason = str(payload.get("reason") or "")
    question = str(getattr(pending, "question", "") or "允许执行这个权限操作吗？")

    headline = "permission requested"
    if action and target:
        headline = f"{headline}  {action} {target}"
    elif action:
        headline = f"{headline}  {action}"
    elif target:
        headline = f"{headline}  {target}"
    lines = [headline]
    if reason:
        lines.append(f"  {reason}")
    elif not any((action, target)):
        lines.append(f"  {question}")

    options = list(getattr(pending, "options", []) or [])
    if options:
        choices: list[str] = []
        for index, option in enumerate(options, start=1):
            label = str(getattr(option, "label", "") or getattr(option, "id", ""))
            option_id = str(getattr(option, "id", ""))
            rendered = _permission_option_label(label, option_id)
            choices.append(f"[{index}] {rendered}")
        lines.append("  " + "  ".join(choices))
    else:
        lines.append("  [1] deny  [2] allow once  [3] allow always")
    return "\n".join(lines)


def _permission_option_label(label: str, option_id: str) -> str:
    normalized = (option_id or label).strip().lower().replace("_", " ")
    aliases = {
        "deny": "deny",
        "allow once": "allow once",
        "allow always same scope": "allow always",
    }
    return aliases.get(normalized, label.strip().lower() or option_id)


def _looks_like_markdown_response(line: str) -> bool:
    return not _looks_like_tool_display_line(line)


def _looks_like_tool_display_line(line: str) -> bool:
    return line.startswith(("Tool call:", "Tool result:"))


def _display_line_kind(line: str) -> TuiEntryKind:
    if line.startswith(("Tool call:", "Tool result:")):
        return TuiEntryKind.TOOL
    return TuiEntryKind.SYSTEM


def _display_line_status(line: str) -> str | None:
    if line.startswith("Tool call:"):
        return "running"
    if line.startswith("Tool result:"):
        return "success"
    return None


def _entry_classes(entry: TuiTranscriptEntry) -> str:
    base = "message"
    if entry.kind == TuiEntryKind.SYSTEM:
        return f"{base} system-message"
    if entry.kind == TuiEntryKind.COMMAND:
        return f"{base} command-message"
    if entry.kind == TuiEntryKind.USER:
        return f"{base} user-message"
    if entry.kind == TuiEntryKind.ASSISTANT:
        return f"{base} assistant-message"
    if entry.kind == TuiEntryKind.REASONING:
        return f"{base} reasoning-message"
    if entry.kind == TuiEntryKind.PERMISSION:
        if entry.status == "permission_requested":
            return f"{base} permission-message permission-requested"
        return f"{base} permission-message"
    if entry.kind == TuiEntryKind.ERROR:
        return f"{base} error-message"
    if entry.kind == TuiEntryKind.TOOL:
        if entry.status == "running":
            return f"{base} tool-message tool-running"
        if entry.status == "success":
            return f"{base} tool-message tool-done"
        if entry.status in {"error", "denied", "failed"}:
            return f"{base} tool-message tool-failed"
        if entry.status == "skipped":
            return f"{base} tool-message tool-skipped"
        return f"{base} tool-message"
    return f"{base} system-message"


def _entry_plain_text(entry: TuiTranscriptEntry) -> str:
    if entry.kind in {TuiEntryKind.USER, TuiEntryKind.ASSISTANT, TuiEntryKind.TOOL, TuiEntryKind.REASONING}:
        return f"{entry.label}\n  {entry.body}"
    return entry.body


def _entry_markdown_text(entry: TuiTranscriptEntry) -> str:
    return f"{entry.label}\n\n{entry.body}"


def _display_line_classes(line: str) -> str:
    if line.startswith("Tool call:"):
        return "message tool-message tool-running"
    if line.startswith("Tool result:"):
        return "message tool-message tool-done"
    return "message system-message"


def _tool_event_classes(event) -> str:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        return "message tool-message tool-running"
    if kind == "finished":
        result = getattr(event, "result", None)
        if getattr(result, "ok", False):
            return "message tool-message tool-done"
        return "message tool-message tool-failed"
    if kind == "permission_requested":
        return "message permission-message"
    if kind == "denied":
        return "message tool-message tool-failed"
    return "message tool-message"


def _tool_event_status(event) -> str | None:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        return "running"
    if kind == "finished":
        result = getattr(event, "result", None)
        return "success" if getattr(result, "ok", False) else "error"
    if kind == "permission_requested":
        return "permission_requested"
    if kind == "denied":
        return "denied"
    if kind == "skipped":
        return "skipped"
    return None


def _tool_event_entry_kind(event) -> TuiEntryKind:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "permission_requested":
        return TuiEntryKind.PERMISSION
    return TuiEntryKind.TOOL


def _tool_event_label(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    status = _tool_event_status(event)
    if status == "permission_requested":
        return "permission requested"
    return f"tool {name} {status}" if status else f"tool {name}"


def _tool_activity_summary(event) -> str:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        tool_call = getattr(event, "tool_call", None)
        return _compact_tool_arguments(getattr(tool_call, "arguments", None))
    if kind == "finished":
        result = getattr(event, "result", None)
        return _compact_tool_content(str(getattr(result, "content", "") or ""))
    return ""


def _activity_line_text(name: str, status: str) -> str:
    if status == "running":
        return f"running · {name}"
    if status == "success":
        return _post_tool_reasoning_text(name)
    if status == "permission_requested":
        return "waiting · permission"
    if status in {"error", "failed"}:
        return f"error · {name}"
    return f"{status} · {name}"


def _post_tool_reasoning_text(name: str) -> str:
    return f"reading {name} result"


def _todo_panel_text(todos: list[TuiTodoItem]) -> str:
    lines = ["Todo"]
    for item in todos:
        marker = "[ ]"
        if item.status == "done":
            marker = "[x]"
        elif item.status == "in_progress":
            marker = "[~]"
        lines.append(f"{marker} {item.content}")
    return "\n".join(lines)


def _tool_status_text(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        arguments = _compact_tool_arguments(getattr(tool_call, "arguments", None))
        suffix = f" {arguments}" if arguments else ""
        return f"正在调用工具：{name}{suffix}"
    if kind == "finished":
        result = getattr(event, "result", None)
        status = "完成" if getattr(result, "ok", False) else "失败"
        content = _compact_tool_content(str(getattr(result, "content", "") or ""))
        suffix = f"：{content}" if content else ""
        return f"工具{status}：{name}{suffix}"
    if kind == "permission_requested":
        request = getattr(event, "permission_request", None)
        target = str(getattr(request, "target", "") or "")
        action = str(getattr(request, "action", "") or "")
        suffix = f"  {action} {target}".rstrip() if action or target else f"  {name}"
        return f"permission requested{suffix}"
    if kind == "denied":
        return f"工具已拒绝：{name}"
    if kind == "skipped":
        return f"工具已跳过：{name}"
    return ""


def _compact_tool_arguments(arguments) -> str:
    if not arguments:
        return ""
    rendered = str(arguments)
    return _compact_tool_content(rendered, max_chars=120)


def _compact_tool_content(text: str, max_chars: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3] + "..."
