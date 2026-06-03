"""FirstCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天通过注入的 chat runner 处理，避免 Textual widget 直接依赖 provider/agent 细节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from firstcoder.app.commands import CommandResult


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


class FirstCoderApp(App[None]):
    """最小 TUI 外壳。"""

    BINDINGS = [("ctrl+c", "quit", "Quit")]

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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="output", wrap=True)
            yield Input(placeholder="输入消息，或使用 /context、/compact status、/compact", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.config.title
        self._refresh_session_subtitle()
        output = self.query_one("#output", RichLog)
        output.write(
            "FirstCoder ready. Commands: /sessions, /session, /resume, /share, /rename, "
            "/context, /compact status, /compact"
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        output = self.query_one("#output", RichLog)
        output.write(f"> {text}")

        if text.startswith("/"):
            if self.command_handler is None:
                output.write("Command handler is not configured.")
                return

            result = self.command_handler.handle(text)
            if result.handled:
                output.write(result.output)
                self._refresh_session_subtitle()
                return
            output.write(f"Unknown command: {text}")
            return

        if self.chat_runner is None:
            output.write("普通聊天入口尚未接入 AgentLoop。")
            return

        if self._chat_busy:
            output.write("Chat is still running. Please wait for the current turn to finish.")
            return

        self._chat_busy = True
        self.run_worker(self._run_chat_turn(text))

    async def _run_chat_turn(self, text: str) -> None:
        output = self.query_one("#output", RichLog)
        try:
            async_runner = getattr(self.chat_runner, "arun_user_turn", None) if self.chat_runner else None
            if async_runner is not None:
                response = await async_runner(text)
            else:
                response = self.chat_runner.run_user_turn(text)
        except Exception as exc:
            output.write(f"Chat error: {exc}")
            self._refresh_session_subtitle()
            return
        finally:
            self._chat_busy = False

        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        if display_lines:
            for line in display_lines:
                output.write(line)
        else:
            content = getattr(response, "content", "")
            output.write(content or "[assistant response has no text content]")
        self._refresh_session_subtitle()

    def _refresh_session_subtitle(self) -> None:
        if self.current_session is None:
            return
        self.sub_title = f"Session: {self.current_session.session_id}"
