"""FirstCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天通过注入的 chat runner 处理，避免 Textual widget 直接依赖 provider/agent 细节。
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
from inspect import isawaitable
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import anyio

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static, TextArea

from firstcoder.app.ports import ChatRunnerLike, CommandHandlerLike, CurrentSessionLike
from firstcoder.app.picker import TuiPickerState, render_picker
from firstcoder.app.picker_adapters import (
    model_picker_item,
    picker_command,
    render_picker_item,
    session_picker_item,
    skill_picker_item,
)
from firstcoder.app.session_commands import SESSION_LIST_VISIBLE_LIMIT
from firstcoder.app.permission_view import permission_choice_for_text, permission_options_text
from firstcoder.app.review_view import review_command_from_text
from firstcoder.app.transcript_view import (
    display_line_kind,
    display_line_status,
    entry_plain_text,
    looks_like_markdown_response,
    looks_like_tool_display_line,
    normalize_stream_text,
)
from firstcoder.app import yuren_topbar_themes
from firstcoder.app.activity_view import compact_tool_arguments, compact_tool_content
from firstcoder.app.tui_state import TuiEntryKind, TuiTaskPlanPanelState, TuiTranscript, TuiTranscriptEntry
from firstcoder.app.topbar_view import _provider_name_markup, _provider_model_markup
from firstcoder.app.tui_view import FirstCoderViewMixin, _entry_renderable
from firstcoder.app.tui_widgets import (
    ComposerTextArea,
    FirstCoderMarkdown,
    FirstCoderScreen,
    FirstCoderTuiConfig,
    _observe_markdown_update,
    _plain_static,
)
from firstcoder.input.attachments import UserAttachment, format_attachment_chip, resolve_paste_attachments

__all__ = [
    "ComposerTextArea",
    "FirstCoderApp",
    "FirstCoderMarkdown",
    "FirstCoderTuiConfig",
    "_entry_renderable",
    "_observe_markdown_update",
    "_plain_static",
    "_provider_model_markup",
    "_provider_name_markup",
]


@dataclass(slots=True)
class _ActiveChatTurn:
    """当前聊天 turn 的身份与令牌。

    `token` 与 `FirstCoderApp._chat_turn_token` 配合做 stale 检测：打断或开启新 turn
    会递增全局令牌，旧 worker 回调里的 token 随之失效，晚到的事件被静默丢弃。
    """

    id: str
    token: int


class FirstCoderApp(FirstCoderViewMixin, App[None]):
    """最小 TUI 外壳。

    渲染与事件流助手拆在 `FirstCoderViewMixin`(tui_view.py)，这里只保留输入分发、
    命令 action 路由、picker 交互、turn 生命周期(令牌/打断/权限恢复)。
    """

    CSS_PATH = "tui.tcss"
    ALLOW_SELECT = True
    BINDINGS = [
        ("ctrl+c", "copy_output_or_quit", "Copy output or quit"),
    ]
    STREAM_RENDER_INTERVAL_SECONDS = 0.2
    WORKING_ANIMATION_INTERVAL_SECONDS = 0.18
    WORKING_FRAMES = ("[.  ]", "[.. ]", "[...]", "[ ..]", "[  .]")
    ESC_INTERRUPT_WINDOW_SECONDS = 1.0
    ACTIVITY_ANIMATION_INTERVAL_SECONDS = 0.24
    WELCOME_PARTICLE_INTERVAL_SECONDS = 0.85
    PROVIDER_GLOW_INTERVAL_SECONDS = yuren_topbar_themes.GLOW_INTERVAL_SECONDS
    COMPACT_WELCOME_MAX_WIDTH = 80
    COMPACT_WELCOME_MAX_HEIGHT = 24
    ACTIVITY_FRAMES = {
        "running": ("[=   ]", "[==  ]", "[=== ]", "[ ===]", "[  ==]", "[   =]"),
        "streaming": ("[>   ]", "[>>  ]", "[>>> ]", "[ >>>]", "[  >>]", "[   >]"),
    }

    def get_default_screen(self) -> Screen:
        return FirstCoderScreen(id="_default")

    def __init__(
        self,
        *,
        command_handler: CommandHandlerLike | None = None,
        chat_runner: ChatRunnerLike | None = None,
        current_session: CurrentSessionLike | None = None,
        config: FirstCoderTuiConfig | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        # 外部注入的端口: 命令路由 / 聊天执行 / 当前会话, 均见 ports.py 的 Protocol。
        self.command_handler = command_handler
        self.chat_runner = chat_runner
        self.current_session = current_session
        self.config = config or FirstCoderTuiConfig()
        self._on_shutdown = on_shutdown
        self._shutdown_called = False
        # turn 生命周期: busy/worker 互斥, _chat_turn_token 递增即作废旧 worker 的回调。
        self._chat_busy = False
        self._chat_worker = None
        self._compact_worker = None
        self._chat_turn_token = 0
        self._active_chat_turn: _ActiveChatTurn | None = None
        self._last_escape_at = 0.0
        # 流式渲染状态: worker 线程入队 delta, UI 线程按 _stream_event_generation 防抖 drain。
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry: TuiTranscriptEntry | None = None
        self._stream_rendered_text = ""
        self._stream_flush_timer: Timer | None = None
        self._stream_markdown_update = None
        self._stream_finalizations: dict[FirstCoderMarkdown, object] = {}
        self._finalized_stream_widgets: set[FirstCoderMarkdown] = set()
        self._stream_event_lock = threading.Lock()
        self._stream_event_generation = 0
        self._stream_event_dispatch_scheduled = False
        self._pending_stream_text: list[str] = []
        self._pending_reasoning_text: list[str] = []
        self._reasoning_buffer = ""
        self._reasoning_is_fallback = False
        # 动画状态: working(思考中) 与 activity(工具/流式) 各一套帧计时器。
        self._working_text = ""
        self._working_frame_index = 0
        self._working_timer: Timer | None = None
        self._activity_animation_kind = ""
        self._activity_animation_detail = ""
        self._activity_frame_index = 0
        self._activity_started_at = 0.0
        self._activity_timer: Timer | None = None
        self._turn_started_at = 0.0
        self._turn_tool_count = 0
        self._running_tool_call_ids: set[str] = set()
        self._live_tool_events_seen = False
        self._stream_segment_closed_for_tool = False
        self._activity_text = "idle · ready"
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        # picker(会话/模型/技能选择) 与写前审查的展开状态, 都是纯 UI 状态。
        self._picker: TuiPickerState | None = None
        self._review_expanded_paths: set[str] = set()
        self._staged_attachments: list[UserAttachment] = []
        self._welcome_widget: Static | None = None
        self._welcome_particle_timer: Timer | None = None
        self._welcome_particle_frame = 0
        self._provider_glow_timer: Timer | None = None
        self._provider_glow_frame = 0
        self.transcript = TuiTranscript()
        self.task_plan_panel_state = TuiTaskPlanPanelState()

    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar", classes="topbar")
        with Vertical(id="main"):
            yield VerticalScroll(id="output")
            yield _plain_static("", id="task-plan-panel", classes="task-plan-panel hidden")
            yield Static("idle · ready", id="activity", classes="activity-line")
            with Vertical(id="composer", classes="composer"):
                yield ComposerTextArea(
                    placeholder="输入消息，Enter 发送，Shift+Enter 换行，Ctrl/Cmd+V 粘贴图片",
                    id="input",
                    show_line_numbers=False,
                    soft_wrap=True,
                    compact=True,
                )

    def on_mount(self) -> None:
        self.title = self.config.title
        self._refresh_session_subtitle()
        self._show_welcome()
        self._sync_provider_glow()

    def _on_terminal_resized(self) -> None:
        """终端尺寸变化已被 Textual 应用后, 刷新顶栏与欢迎页布局。"""
        self._refresh_session_subtitle()
        self._refresh_welcome_layout()

    def on_unmount(self) -> None:
        self._stop_welcome_particles()
        self._stop_provider_glow()
        # 关闭钩子只触发一次, 重复 unmount(如测试重建 app) 不重复清理运行时。
        if not self._shutdown_called and self._on_shutdown is not None:
            self._shutdown_called = True
            self._on_shutdown()

    async def _submit_composer(self) -> None:
        """输入框提交的总分发: picker 数字选择 > slash command > 普通聊天。

        picker 激活时纯数字输入被当作选择消费掉(此时无法发送纯数字聊天内容);
        其余输入先写入 transcript, 再按是否 "/" 开头分流到命令层或聊天层。
        """
        input_widget = self.query_one("#input", TextArea)
        text = input_widget.text.strip()
        input_widget.clear()
        attachments = list(self._staged_attachments)
        if not text and not attachments:
            return
        if not text:
            # 只粘贴了附件没有文字时, 给模型一句默认指令。
            text = "请分析这些附件。"
        self._dismiss_welcome()
        self._record_input_history(text)

        if self._picker is not None and text.isdigit():
            # picker 激活时纯数字优先视为选择项编号, 而不是聊天内容。
            if self._picker_select_number(int(text)):
                return

        attachment_chips = "\n".join(format_attachment_chip(item) for item in attachments)
        user_display = f"> {text}"
        if attachment_chips:
            user_display = f"{user_display}\n{attachment_chips}"
        self._write_line(user_display, kind=TuiEntryKind.USER)

        if text.startswith("/"):
            if self.command_handler is None:
                self._write_line("Command handler is not configured.", kind=TuiEntryKind.ERROR)
                return

            if " ".join(text.split()) == "/compact":
                self._submit_manual_compact(text)
                return

            result = self.command_handler.handle(text)
            if result.handled:
                self._write_line(result.output, kind=TuiEntryKind.COMMAND)
                self._handle_command_action(result.action, output=result.output)
                self._refresh_session_subtitle()
                return
            self._write_line(f"Unknown command: {text}", kind=TuiEntryKind.ERROR)
            return

        self._staged_attachments.clear()
        self._submit_chat_text(text, attachments=attachments)

    def _submit_manual_compact(self, text: str) -> None:
        """手动 /compact: 与聊天 turn 互斥, 放进 worker 里跑, 避免压缩期间阻塞 UI。

        command handler 是同步函数且内部可能调模型做摘要, 所以 worker 里再经
        anyio.to_thread 甩到线程执行。
        """
        if self._chat_busy:
            self._write_line("Chat is still running. Please wait before compacting context.", kind=TuiEntryKind.SYSTEM)
            return
        if self._compact_worker is not None:
            self._write_line("Manual compact is already running.", kind=TuiEntryKind.SYSTEM)
            return
        self._set_activity("compacting context...")
        self._write_line("Manual compact started.", kind=TuiEntryKind.SYSTEM)
        self._compact_worker = self.run_worker(self._run_manual_compact_command(text))

    async def _run_manual_compact_command(self, text: str) -> None:
        try:
            assert self.command_handler is not None
            result = await anyio.to_thread.run_sync(self.command_handler.handle, text)
        except asyncio.CancelledError:
            # worker 被取消(退出/打断)属正常路径, 静默退出。
            return
        except Exception as exc:
            self._write_line(f"Manual compact error: {exc}", kind=TuiEntryKind.ERROR)
            return
        finally:
            self._compact_worker = None

        if result.handled:
            self._write_line(result.output, kind=TuiEntryKind.COMMAND)
            self._handle_command_action(result.action, output=result.output)
            self._refresh_session_subtitle()
        else:
            self._write_line(f"Unknown command: {text}", kind=TuiEntryKind.ERROR)
        self._set_activity("done")

    async def on_composer_text_area_submitted(self, event: ComposerTextArea.Submitted) -> None:
        """Enter 提交: picker 激活时 Enter 意为"选中当前项", 否则走正常提交。"""
        event.stop()
        if self._picker is not None:
            self._picker_select_index(self._picker.selected_index)
            return
        await self._submit_composer()

    def on_key(self, event: Key) -> None:
        """全局按键路由: picker 按键 > Esc 打断 > 输入框聚焦时的历史召回(上下键)。"""
        if self._picker is not None and self._handle_picker_key(event):
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            if self._handle_escape_interrupt():
                event.stop()
                event.prevent_default()
            return
        if event.key not in {"up", "down"}:
            return
        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        input_widget = self.query_one("#input", TextArea)
        recalled = self._recall_input_history(event.key)
        if recalled is None:
            return
        event.stop()
        event.prevent_default()
        input_widget.load_text(recalled)
        input_widget.cursor_location = input_widget.document.end

    async def action_copy_output_or_quit(self) -> None:
        """Ctrl+C: 有选区先复制, 无选区才保留为退出。"""

        focused = self.focused
        if isinstance(focused, TextArea) and focused.selected_text:
            focused.action_copy()
            return
        selected_text = self.screen.get_selected_text()
        if selected_text is not None:
            self.copy_to_clipboard(selected_text)
            return
        result = self.action_quit()
        if isawaitable(result):
            await result

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        # Textual 的剪贴板走 OSC52, macOS 上部分终端不生效, 用 pbcopy 兜底。
        if platform.system() == "Darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=False)

    def on_paste(self, event: events.Paste) -> None:
        """把粘贴的文件路径或剪贴板图片转为待发送附件。"""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        if self._stage_paste_attachments(getattr(event, "text", None)):
            event.stop()
            event.prevent_default()

    def _paste_composer_clipboard_image(self) -> bool:
        """焦点在输入框时, 处理 ComposerTextArea 转发来的 Ctrl/Cmd+V 图片粘贴。"""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return False
        return self._stage_paste_attachments(None)

    def _notify_clipboard_image_unavailable(self) -> None:
        """剪贴板没有图片时给用户一个明确反馈, 确认快捷键已生效。"""

        self._write_line(
            "No clipboard image found. Copy an image first, or paste an image file path instead.",
            kind=TuiEntryKind.SYSTEM,
        )

    def _stage_paste_attachments(self, paste_text: str | None) -> bool:
        """解析粘贴内容为附件并暂存; 返回 True 表示该粘贴事件已被附件路径消费。

        按 path 去重, 重复粘贴同一张图不会堆叠; 每次成功暂存输出 chip 提示。
        """
        try:
            attachments = resolve_paste_attachments(paste_text)
        except (OSError, ValueError) as exc:
            self._write_line(f"Could not attach pasted image: {exc}", kind=TuiEntryKind.ERROR)
            return True
        if not attachments:
            return False
        existing_paths = {item.path for item in self._staged_attachments}
        added = [item for item in attachments if item.path not in existing_paths]
        if not added:
            return True
        self._staged_attachments.extend(added)
        chips = ", ".join(format_attachment_chip(item) for item in added)
        self._write_line(f"Attached: {chips}", kind=TuiEntryKind.SYSTEM)
        return True

    def _next_chat_turn_token(self) -> int:
        self._chat_turn_token += 1
        return self._chat_turn_token

    def _begin_active_chat_turn(self) -> int:
        """开启新 turn: 递增令牌(旧 worker 回调全部失效)并重置耗时/工具计数。"""
        token = self._next_chat_turn_token()
        self._start_turn_metrics()
        self._active_chat_turn = _ActiveChatTurn(
            id=uuid4().hex,
            token=token,
        )
        return token

    def _resume_active_chat_turn(self) -> int:
        """权限确认后恢复原 turn: 保留 turn 身份与耗时统计, 但换新令牌。

        等待用户回答期间旧 worker 已结束; 回答后恢复执行时若沿用过期令牌, 任何
        期间发生的打断都会让新 worker 的回调被误判为 stale, 所以必须发新令牌。
        """
        active_turn = self._active_chat_turn
        if active_turn is not None:
            token = self._next_chat_turn_token()
            active_turn.token = token
            self._preserve_turn_metrics()
            return token
        return self._begin_active_chat_turn()

    def _is_current_chat_turn(self, token: int) -> bool:
        return token == self._chat_turn_token

    def _finish_chat_turn(self, token: int) -> None:
        """worker 收尾: 仅当令牌仍有效时复位 busy/worker。

        存在 last_pending_input(等待权限确认)时保留 _active_chat_turn, 因为回答后
        还要在同一 turn 里恢复执行。
        """
        if not self._is_current_chat_turn(token):
            return
        self._refresh_task_plan_panel_from_current_session()
        self._chat_busy = False
        self._chat_worker = None
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._active_chat_turn = None

    def _handle_escape_interrupt(self) -> bool:
        """Esc 打断: chat 空闲时不消费; 运行中要求窗口期内连按两次确认。"""
        if not self._chat_busy:
            self._last_escape_at = 0.0
            return False
        now = time.monotonic()
        if now - self._last_escape_at > self.ESC_INTERRUPT_WINDOW_SECONDS:
            self._last_escape_at = now
            self._set_activity("running · press Esc again to interrupt")
            return True
        self._last_escape_at = 0.0
        self._interrupt_chat_turn()
        return True

    def _interrupt_chat_turn(self) -> None:
        """打断当前 turn: 令牌 +1 使旧 worker 的所有回调变 stale, 再取消执行。

        三道保险: 递增 token(UI 侧丢弃晚到事件) -> 丢弃已入队的流式 delta ->
        通知 chat_runner 取消(agent 侧停止) -> 取消 Textual worker(协程侧停止)。
        """
        self._chat_turn_token += 1
        self._discard_stream_deltas()
        cancel_current_turn = getattr(self.chat_runner, "cancel_current_turn", None)
        if cancel_current_turn is not None:
            cancel_current_turn()
        worker = self._chat_worker
        self._chat_worker = None
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        self._chat_busy = False
        self._active_chat_turn = None
        self._running_tool_call_ids.clear()
        self._stop_working_animation()
        self._stop_activity_animation()
        self._set_activity("interrupted")
        self._write_line("Interrupted current turn.", kind=TuiEntryKind.SYSTEM)

    def _record_input_history(self, text: str) -> None:
        # 与上一条相同则不重复入栈, 避免连按提交刷出重复历史。
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_index = None

    def _recall_input_history(self, direction: str) -> str | None:
        if not self._input_history:
            return None
        if direction == "up":
            if self._input_history_index is None:
                self._input_history_index = len(self._input_history) - 1
            else:
                self._input_history_index = max(0, self._input_history_index - 1)
            return self._input_history[self._input_history_index]
        if direction == "down":
            if self._input_history_index is None:
                return None
            if self._input_history_index >= len(self._input_history) - 1:
                self._input_history_index = None
                return ""
            self._input_history_index += 1
            return self._input_history[self._input_history_index]
        return None

    def _submit_chat_text(self, text: str, *, attachments: list[UserAttachment] | None = None) -> None:
        """普通聊天的入口路由, 按当前状态分四路。

        1. chat 忙: 不排队新 turn, 而是把文本作为 guidance 注入运行中的 turn;
        2. 有挂起的权限确认: 先匹配写前审查的纯 UI 命令(review all/clear/<path>,
        只改展开状态不进会话), 再按 1/2/3/4 解析为权限选择, 无法解析就重打选项
        重问, 解析成功则恢复原 turn;
        3. 以上都不是: 开新 turn。
        """
        if self.chat_runner is None:
            self._write_line("普通聊天入口尚未接入 AgentLoop。", kind=TuiEntryKind.ERROR)
            return

        if self._compact_worker is not None:
            self._write_line("Manual compact is still running. Please wait before sending a chat message.", kind=TuiEntryKind.SYSTEM)
            return

        if self._chat_busy:
            add_guidance = getattr(self.chat_runner, "add_guidance", None)
            if add_guidance is None:
                self._write_line(
                    "Chat is still running. Please wait for the current turn to finish.",
                    kind=TuiEntryKind.SYSTEM,
                )
                return
            add_guidance(text)
            self._write_line("Guidance queued for the running turn.", kind=TuiEntryKind.SYSTEM)
            self._set_activity("running · guidance queued")
            return

        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is not None and getattr(pending, "kind", None) == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                review_command = review_command_from_text(text, review_payload)
                if review_command is not None:
                    action, path = review_command
                    if action == "all":
                        self._review_expanded_paths = {str(item.get("path") or "") for item in review_payload.get("files", []) if isinstance(item, dict)}
                    elif action == "clear":
                        self._review_expanded_paths.clear()
                    elif path:
                        self._review_expanded_paths.add(path)
                    self._write_review_payload(review_payload)
                    return
            choice = permission_choice_for_text(text, pending)
            if choice is None:
                # 不是有效选项: 重打选项卡等用户再答, 不推进任何状态。
                self._write_line(permission_options_text(pending), kind=TuiEntryKind.PERMISSION)
                return
            self._chat_busy = True
            token = self._resume_active_chat_turn()
            self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, choice, token))
            return

        self._chat_busy = True
        token = self._begin_active_chat_turn()
        self._chat_worker = self.run_worker(self._run_chat_turn(text, token, attachments=attachments))

    def _handle_command_action(self, action: dict[str, Any] | None, *, output: str = "") -> bool:
        """执行命令层经 CommandResult.action 带上来的 UI 副作用。

        action 是命令层(纯逻辑, 不碰 widget) 通知 TUI 做事的唯一通道:
        submit_chat 注入文本当用户消息; new_session 清屏; *_picker 打开选择器;
        replay_session 重放会话; model_changed 刷顶栏; skill_referenced 把技能
        引用插进输入框。
        """
        if not action:
            return False
        action_type = action.get("type")
        if action_type == "submit_chat":
            text = str(action.get("text") or "").strip()
            if text:
                self._submit_chat_text(text)
            return True
        if action_type == "new_session":
            self._picker = None
            self._clear_output()
            if output:
                self._write_line(output, kind=TuiEntryKind.COMMAND)
            return False
        picker_specs = {
            "resume_picker": (
                "resume",
                "Select a session:",
                "sessions",
                session_picker_item,
                "No sessions.",
                "Use up/down and enter to resume, or type a number.",
                "sessions",
            ),
            "model_picker": (
                "model",
                "Select a model:",
                "models",
                model_picker_item,
                "No model choices.",
                "Use up/down and enter to switch, or type /model <provider>/<model>.",
                "models",
            ),
            "skill_picker": (
                "skill",
                "Select a skill:",
                "skills",
                skill_picker_item,
                "No skills.",
                "Use up/down and enter to reference, or type a number.",
                "skills",
            ),
        }
        picker_spec = picker_specs.get(action_type) if isinstance(action_type, str) else None
        if picker_spec is not None:
            kind, title, items_key, item_factory, empty_text, footer, count_label = picker_spec
            self._open_picker(
                kind=kind,
                title=title,
                items=[item_factory(item) for item in action.get(items_key, []) if isinstance(item, dict)],
                selected_index=int(action.get("selected_index") or 0),
                empty_text=empty_text,
                footer=footer,
                count_label=count_label,
            )
            return False
        if action_type == "replay_session":
            self._picker = None
            self._replay_current_session()
            return False
        if action_type == "model_changed":
            self._picker = None
            self.config.provider_name = str(action.get("provider") or "")
            self.config.provider_model = str(action.get("model") or "")
            self._sync_provider_glow()
            return False
        if action_type == "skill_referenced":
            self._picker = None
            self._insert_input_text(str(action.get("reference") or ""))
            return False
        return False

    def _handle_picker_key(self, event: Key) -> bool:
        picker = self._picker
        if picker is None:
            return False
        if event.key == "up":
            picker.move(-1)
            self._render_picker()
            return True
        if event.key == "down":
            picker.move(1)
            self._render_picker()
            return True
        if event.key == "enter":
            self._picker_select_index(picker.selected_index)
            return True
        if event.key == "escape":
            kind = picker.kind
            self._picker = None
            self._write_line(f"{kind.capitalize()} selection cancelled.", kind=TuiEntryKind.COMMAND)
            return True
        return False

    def _picker_select_number(self, number: int) -> bool:
        picker = self._picker
        if picker is None:
            return False
        index = number - 1
        if index < 0 or index >= len(picker.items):
            self._write_line("Invalid selection.", kind=TuiEntryKind.ERROR)
            return True
        self._picker_select_index(index)
        return True

    def _picker_select_index(self, index: int) -> None:
        """选中 picker 项: 回译成对应 slash command 走命令层, 复用同一套命令路径。"""
        picker = self._picker
        if picker is None or self.command_handler is None:
            return
        if index < 0 or index >= len(picker.items):
            return
        item = picker.items[index]
        command = picker_command(picker.kind, item)
        if not command:
            return
        result = self.command_handler.handle(command)
        if result.output:
            self._write_line(result.output, kind=TuiEntryKind.COMMAND)
        self._handle_command_action(result.action)
        self._refresh_session_subtitle()

    def _open_picker(self, **fields) -> None:
        self._picker = TuiPickerState(**fields)
        self._render_picker()

    def _render_picker(self) -> None:
        picker = self._picker
        if picker is None:
            return
        self._replace_last_command_output(
            render_picker(
                picker,
                limit=SESSION_LIST_VISIBLE_LIMIT,
                render_item=lambda item, index: render_picker_item(picker, item, index),
            )
        )

    def _insert_input_text(self, text: str) -> None:
        if not text:
            return
        input_widget = self.query_one("#input", TextArea)
        existing = input_widget.text
        prefix = "" if not existing or existing.endswith((" ", "\n")) else " "
        input_widget.load_text(f"{existing}{prefix}{text}")
        input_widget.cursor_location = input_widget.document.end
        input_widget.focus()

    def _replace_last_command_output(self, text: str) -> None:
        """原地更新最后一条 COMMAND 条目(picker 重绘用), 找不到就追加一行。

        entry.widget 为 None 说明输出区是测试替身, 只能整段重渲 transcript。
        """
        for entry in reversed(self.transcript.entries):
            if entry.kind == TuiEntryKind.COMMAND:
                entry.body = text
                rendered = entry_plain_text(entry)
                widget = entry.widget
                if widget is not None and hasattr(widget, "update"):
                    widget.update(_entry_renderable(entry, rendered))
                    return
                self._rerender_transcript()
                return
        self._write_line(text, kind=TuiEntryKind.COMMAND)

    def _clear_output(self) -> None:
        self._clear_task_plan_panel_if_mounted()
        self.transcript = TuiTranscript()
        self.task_plan_panel_state = TuiTaskPlanPanelState()
        self._remove_output_children()

    def _rerender_transcript(self) -> None:
        """清空输出区后按 transcript 条目重建; ASSISTANT 条目走 Markdown 渲染。"""
        entries = list(self.transcript.entries)
        self.transcript = TuiTranscript()
        self._remove_output_children()
        for entry in entries:
            if entry.kind == TuiEntryKind.ASSISTANT:
                self._write_markdown_message(entry.body)
            else:
                self._write_line(entry.body, kind=entry.kind, label=entry.label, status=entry.status)

    def _remove_output_children(self) -> None:
        # 真实 Textual 容器有 remove_children; 测试替身只有 children 列表, 逐个 remove。
        output = self.query_one("#output")
        if hasattr(output, "remove_children"):
            output.remove_children()
            return
        if hasattr(output, "children"):
            for child in list(output.children):
                remove = getattr(child, "remove", None)
                if remove is not None:
                    remove()

    def _replay_current_session(self) -> None:
        """resume/fork 后从 session view 重建整屏渲染。

        view 是会话历史的投影: user 消息前缀 ">", assistant 文本走 Markdown,
        tool_call/tool_result 压成一行工具状态。重放完同步 pending input 并重画
        权限卡, 保证恢复到"等待权限确认"的会话时 UI 也停在同一步。
        """
        current_session = self.current_session
        if current_session is None:
            return
        rebuild_view = getattr(current_session, "rebuild_view", None)
        if rebuild_view is None:
            return
        view = rebuild_view()
        self._clear_output()
        if view.task_plan is not None:
            self._render_task_plan_panel(view.task_plan)
        for message in getattr(view, "messages", []):
            if message.role == "user":
                content = "\n".join(part.content for part in message.parts if getattr(part, "content", ""))
                if not content:
                    continue
                self._write_line(f"> {content}", kind=TuiEntryKind.USER)
            elif message.role == "assistant":
                for part in message.parts:
                    if part.kind == "text" and part.content:
                        self._write_markdown_message(part.content)
                    elif part.kind == "tool_call":
                        name = str(part.metadata.get("tool_name") or "tool")
                        arguments = compact_tool_arguments(part.metadata.get("arguments"))
                        suffix = f" {arguments}" if arguments else ""
                        self._write_line(
                            f"正在调用工具：{name}{suffix}",
                            kind=TuiEntryKind.TOOL,
                            label=f"tool {name} running",
                            status="running",
                        )
            else:
                for part in message.parts:
                    if part.kind != "tool_result":
                        continue
                    name = str(part.metadata.get("tool_name") or "tool")
                    ok = bool(part.metadata.get("ok", True))
                    status = "success" if ok else "error"
                    result = compact_tool_content(part.content)
                    suffix = f"：{result}" if result else ""
                    self._write_line(
                        f"工具{'完成' if ok else '失败'}：{name}{suffix}",
                        kind=TuiEntryKind.TOOL,
                        label=f"tool {name} {status}",
                        status=status,
                    )
        sync_pending = getattr(self.chat_runner, "sync_pending_input_from_current_session", None)
        if sync_pending is not None:
            sync_pending()
        self._write_pending_input()

    async def _resume_permission_turn(self, request_id: str, answer: str, token: int) -> None:
        """权限确认回答后的恢复 worker: 重新安装带 token 的 handler 再续跑。

        与 _run_chat_turn 同构: CancelledError(被打断)静默退出; 异常只在令牌仍有
        效时写错误行; finally 恢复旧 handler 并收尾 turn; 响应只在令牌有效时落屏。
        """
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler(token)
            previous_tool_handler = self._install_tool_event_handler(token)
            self._preserve_turn_metrics()
            self._show_working_indicator("resuming with permission answer...")
            assert self.chat_runner is not None
            response = await self.chat_runner.aresume_with_user_input(request_id, answer)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current_chat_turn(token):
                self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token):
            self._write_chat_response(response)

    async def _run_chat_turn(
        self,
        text: str,
        token: int,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> None:
        """一个完整聊天 turn 的 worker: 安装 handler -> 跑 runner -> 校验令牌后落屏。

        handler 在 worker 内安装/恢复而不是常驻, 是因为每个 turn 的 token 不同,
        且恢复旧 handler 可以让测试替身或外层监听者不受污染。
        """
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler(token)
            previous_tool_handler = self._install_tool_event_handler(token)
            if self._active_chat_turn is None:
                self._start_turn_metrics()
                self._active_chat_turn = _ActiveChatTurn(
                    id=uuid4().hex,
                    token=token,
                )
            self._show_working_indicator("planning next step...")
            assert self.chat_runner is not None
            response = (
                await self.chat_runner.arun_user_turn(text, attachments=attachments)
                if attachments
                else await self.chat_runner.arun_user_turn(text)
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current_chat_turn(token):
                self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token):
            self._write_chat_response(response)

    def _write_chat_response(self, response) -> None:
        """turn 结束后落屏: 与已流式渲染的内容去重, 再补 display_lines 里漏掉的行。

        流式期间文本和工具行已经直播过了, display_lines 是 agent 侧的完整记录,
        直接全打会重复, 所以: 流式文本段用 normalize 后的内容比对剔除; 本轮见过
        实时工具事件则剔除 display_lines 里的工具行; 两者都发生时整份跳过。
        """
        self._drain_stream_deltas()
        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        content = getattr(response, "content", "")
        if self._stream_text_started:
            if content and normalize_stream_text(content) != normalize_stream_text(self._stream_text_buffer):
                self._stream_text_buffer = content
                if self._stream_text_entry is not None:
                    self._stream_text_entry.body = content
            display_lines = [line for line in display_lines if looks_like_tool_display_line(line) or normalize_stream_text(line) != normalize_stream_text(self._stream_text_buffer)]
            self._finalize_stream_widget()
        if self._live_tool_events_seen:
            display_lines = [line for line in display_lines if not looks_like_tool_display_line(line)]
        if self._live_tool_events_seen and self._stream_text_started:
            display_lines = []
        if display_lines:
            for line in display_lines:
                if line == content or looks_like_markdown_response(line):
                    self._write_markdown_message(line)
                else:
                    self._write_line(line, kind=display_line_kind(line), status=display_line_status(line))
        elif not self._stream_text_started:
            self._write_markdown_message(content or "[assistant response has no text content]")
        self._write_pending_input()
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._stop_activity_animation()
            self._set_activity("done")
        self._refresh_session_subtitle()

    def _show_activity_animation(self, kind: str, detail: str) -> None:
        self._activity_animation_kind = kind
        self._activity_animation_detail = detail
        self._activity_frame_index = 0
        self._activity_started_at = time.monotonic()
        self._set_activity(self._activity_animation_body())
        self._start_activity_animation()

    def _start_turn_metrics(self) -> None:
        self._turn_started_at = time.monotonic()
        self._turn_tool_count = 0
        self._running_tool_call_ids = set()

    def _turn_elapsed_seconds(self) -> float:
        if not self._turn_started_at:
            return 0.0
        return max(0.0, time.monotonic() - self._turn_started_at)
