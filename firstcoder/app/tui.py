"""FirstCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天暂时不在这里直接接 provider；后续可以把 `AgentLoop.run_user_turn()` 注入进来。
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from firstcoder.app.commands import ContextCommandHandler


@dataclass(slots=True)
class FirstCoderTuiConfig:
    title: str = "FirstCoder"


class FirstCoderApp(App[None]):
    """最小 TUI 外壳。"""

    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(
        self,
        *,
        command_handler: ContextCommandHandler | None = None,
        config: FirstCoderTuiConfig | None = None,
    ) -> None:
        super().__init__()
        self.command_handler = command_handler
        self.config = config or FirstCoderTuiConfig()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="output", wrap=True)
            yield Input(placeholder="输入消息，或使用 /context、/compact status、/compact", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.config.title
        output = self.query_one("#output", RichLog)
        output.write("FirstCoder ready. Commands: /context, /compact status, /compact")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        output = self.query_one("#output", RichLog)
        output.write(f"> {text}")

        if self.command_handler is None:
            output.write("Command handler is not configured.")
            return

        result = self.command_handler.handle(text)
        if result.handled:
            output.write(result.output)
            return

        output.write("普通聊天入口尚未接入 AgentLoop。")
