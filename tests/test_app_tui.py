import asyncio

import pytest

from firstcoder.agent.loop import ToolExecutionEvent
from firstcoder.app.commands import CommandResult
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.agent.user_input import UserInputOption, UserInputRequest
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.app.tui import _observe_markdown_update
from firstcoder.app.tui import _entry_classes, _tool_event_entry_kind, _tool_event_label, _tool_event_status
from firstcoder.app.tui_state import TuiEntryKind, TuiTodoItem, TuiTranscript
from firstcoder.app.tui_state import TuiTranscriptEntry
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.providers.types import ChatResponse, ChatStreamEvent, ToolCall
from firstcoder.tools.types import ToolResult


class FakeOutput:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.mounted: list[object] = []

    def write(self, line: str) -> None:
        if self.lines:
            self.lines[-1] += line
        else:
            self.lines.append(line)

    def write_line(self, line: str) -> None:
        self.lines.append(line)

    def mount(self, widget: object) -> None:
        self.mounted.append(widget)
        if type(widget).__name__ == "Static":
            self.lines.append(str(getattr(widget, "content", getattr(widget, "renderable", ""))))
        if type(widget).__name__ == "Markdown":
            widget.updates = []  # type: ignore[attr-defined]

            def update(markdown: str) -> None:
                widget.updates.append(markdown)  # type: ignore[attr-defined]

            widget.update = update  # type: ignore[method-assign]

    def scroll_end(self, animate: bool = False) -> None:
        return None


class FakeMarkdownUpdateResult:
    def __init__(self, exception: BaseException | None) -> None:
        self._exception = exception
        self.exception_observed = False
        self.callbacks = []
        self._future = self

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def exception(self) -> BaseException | None:
        self.exception_observed = True
        return self._exception

    def finish(self) -> None:
        for callback in self.callbacks:
            callback(self)


class FakeActivity:
    def __init__(self) -> None:
        self.updates: list[str] = []

    def update(self, text: object) -> None:
        plain = getattr(text, "plain", None)
        self.updates.append(str(plain if plain is not None else text))


class FakeTopbar(FakeActivity):
    pass


class FakeTodoPanel(FakeActivity):
    pass


class FakeSession:
    session_id = "sess_test"
    mode = "standard"
    runtime_state = SessionRuntimeState(session_id="sess_test")

    def rebuild_view(self) -> SessionView:
        return SessionView(session_id="sess_test")


def test_firstcoder_app_can_be_created_with_command_handler() -> None:
    handler = ContextCommandHandler(session=FakeSession())

    app = FirstCoderApp(command_handler=handler, config=FirstCoderTuiConfig(title="TestCoder"))

    assert app.command_handler is handler
    assert app.config.title == "TestCoder"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_uses_custom_chrome_instead_of_textual_header_footer() -> None:
    app = FirstCoderApp(current_session=FakeSession())

    async with app.run_test():
        widget_types = [type(widget).__name__ for widget in app.query("*")]
        widget_ids = [getattr(widget, "id", None) for widget in app.query("*")]

    assert "Header" not in widget_types
    assert "Footer" not in widget_types
    assert "topbar" in widget_ids
    assert "main" in widget_ids


def test_firstcoder_app_topbar_text_includes_session_id() -> None:
    app = FirstCoderApp(current_session=FakeSession())

    assert app._topbar_text() == (
        "[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   "
        "[#303238]·[/]   [#6e6d72]sess_test[/]   "
        "[#303238]·[/]   [#6e6d72]standard[/]"
    )


def test_firstcoder_app_topbar_text_includes_provider_model_mode_and_cwd() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="FirstCoder",
        ),
    )

    assert app._topbar_text() == (
        "[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   "
        "[#303238]·[/]   [#6e6d72]sess_test[/]   "
        "[#303238]·[/]   [#6e6d72]yurenapi/gpt-5.5[/]   "
        "[#303238]·[/]   [#6e6d72]standard[/]   [#303238]·[/]   [#6e6d72]cwd FirstCoder[/]"
    )


def test_observe_markdown_update_consumes_cancelled_update_result() -> None:
    result = FakeMarkdownUpdateResult(asyncio.CancelledError())

    _observe_markdown_update(result)
    result.finish()

    assert result.exception_observed is True


def test_observe_markdown_update_does_not_consume_unexpected_update_errors() -> None:
    result = FakeMarkdownUpdateResult(RuntimeError("markdown failed"))

    _observe_markdown_update(result)
    with pytest.raises(RuntimeError, match="markdown failed"):
        result.finish()


def test_firstcoder_app_topbar_uses_spacious_two_sided_layout_when_width_is_known() -> None:
    app = FirstCoderApp(
        current_session=FakeSession(),
        config=FirstCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="FirstCoder",
        ),
    )

    text = app._topbar_text(width=120)

    assert text.startswith("[#7bba55]FirstCoder[/]")
    assert "   [#303238]·[/]   [#6e6d72]sess_test[/]" not in text
    assert "[#7bba55]idle · ready[/]" in text
    assert "[#6e6d72]sess_test[/]" in text
    assert "[#6e6d72]cwd FirstCoder[/]" in text
    assert " " * 20 in text


def test_firstcoder_app_topbar_highlights_bypass_mode_and_truncates_long_session() -> None:
    class BypassSession(FakeSession):
        session_id = "sess_c8d401e2124f"
        mode = "bypass"

    app = FirstCoderApp(current_session=BypassSession())

    assert app._topbar_text() == (
        "[#7bba55]FirstCoder[/]   [#303238]·[/]   [#7bba55]idle · ready[/]   "
        "[#303238]·[/]   [#6e6d72]sess_c8d401e2[/]   "
        "[#303238]·[/]   [#b28443]bypass[/]"
    )


def test_firstcoder_app_topbar_includes_live_activity_status() -> None:
    app = FirstCoderApp(current_session=FakeSession())

    app._activity_text = "thinking [.. ] planning next step..."

    assert "[#7bba55]thinking [.. ] planning next step...[/]" in app._topbar_text(width=120)


def test_tui_transcript_records_structured_entries_with_stable_labels() -> None:
    transcript = TuiTranscript()

    user = transcript.add(TuiEntryKind.USER, "hello")
    assistant = transcript.add(TuiEntryKind.ASSISTANT, "hi")
    tool = transcript.add(
        TuiEntryKind.TOOL,
        "rg -n Permission firstcoder",
        label="tool exec_command running",
        status="running",
    )

    assert [entry.id for entry in transcript.entries] == [user.id, assistant.id, tool.id]
    assert [entry.label for entry in transcript.entries] == ["you", "FirstCoder", "tool exec_command running"]
    assert transcript.entries[-1].status == "running"


def test_tui_transcript_tracks_active_tool_until_terminal_status() -> None:
    transcript = TuiTranscript()

    transcript.record_tool_activity("exec_command", "running", "rg -n Permission firstcoder")

    assert transcript.active_tool is not None
    assert transcript.active_tool.name == "exec_command"
    assert transcript.active_tool.status == "running"
    assert transcript.active_tool.summary == "rg -n Permission firstcoder"

    transcript.record_tool_activity("exec_command", "success", "12 matches")

    assert transcript.active_tool is None
    assert transcript.recent_tools[-1].name == "exec_command"
    assert transcript.recent_tools[-1].status == "success"


def test_tui_transcript_updates_persistent_todos_from_tool_data() -> None:
    transcript = TuiTranscript()

    transcript.update_todos(
        [
            {"id": "todo_1", "content": "读代码", "status": "done"},
            {"id": "todo_2", "content": "跑测试", "status": "in_progress"},
        ]
    )

    assert transcript.todos == [
        TuiTodoItem(id="todo_1", content="读代码", status="done"),
        TuiTodoItem(id="todo_2", content="跑测试", status="in_progress"),
    ]


def test_firstcoder_app_records_rendered_messages_in_transcript(monkeypatch) -> None:
    output = FakeOutput()
    app = FirstCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._write_line("> hello", kind=TuiEntryKind.USER)
    app._write_markdown_message("**hi**")

    assert [(entry.kind, entry.label, entry.body) for entry in app.transcript.entries] == [
        (TuiEntryKind.USER, "you", "> hello"),
        (TuiEntryKind.ASSISTANT, "FirstCoder", "**hi**"),
    ]


class FakeChatRunner:
    def __init__(self) -> None:
        self.inputs = []
        self.last_display_lines = []

    def run_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        return ChatResponse(provider="fake", model="fake", content=f"reply:{content}")


class FakeDisplayChatRunner(FakeChatRunner):
    def run_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.last_display_lines = ["Tool call: echo {}", "Tool result: echo success: ok", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.last_display_lines = ["async reply"]
        return ChatResponse(provider="fake", model="fake", content="async reply")


class FailingAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        raise RuntimeError("provider down")


class FakeStreamingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.seen = []
        self.stream_event_handler = self.seen.append

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="thinking"))
        self.last_display_lines = ["done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeStreamingTextAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.stream_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
        self.last_display_lines = ["hello"]
        return ChatResponse(provider="fake", model="fake", content="hello")


class FakeToolEventAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.tool_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
        self.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
        self.tool_event_handler(
            ToolExecutionEvent(
                kind="finished",
                tool_call=tool_call,
                result=ToolResult(name="echo", ok=True, content="hello"),
            )
        )
        self.last_display_lines = [
            'Tool call: echo {"text": "hello"}',
            "Tool result: echo success: hello",
            "done",
        ]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionResumeRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
            ],
        )
        self.resumes: list[tuple[str, str]] = []

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["Tool result: write success: ok", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionWaitingRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
                UserInputOption(id="allow_always_same_scope", label="Allow always"),
            ],
            payload={
                "action": "write_path",
                "target": "README.md",
                "reason": "写入文件需要用户确认。",
            },
        )


class BlockingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        import anyio

        self.started = anyio.Event()
        self.release = anyio.Event()

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.started.set()
        await self.release.wait()
        return ChatResponse(provider="fake", model="fake", content="done")


class UnhandledCommandHandler:
    def handle(self, text: str) -> CommandResult:
        return CommandResult(handled=False)


def test_firstcoder_app_can_be_created_with_composite_handler_and_chat_runner() -> None:
    context_handler = ContextCommandHandler(session=FakeSession())
    composite = CompositeCommandHandler(
        [
            SessionCommandHandler(catalog=object()),  # constructor storage only; not used by this test
            context_handler,
        ]
    )
    runner = FakeChatRunner()

    app = FirstCoderApp(command_handler=composite, chat_runner=runner)

    assert app.command_handler is composite
    assert app.chat_runner is runner


@pytest.mark.anyio
async def test_firstcoder_app_runs_plain_chat_when_only_chat_runner_is_configured() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
async def test_firstcoder_app_does_not_send_unhandled_slash_command_to_chat_runner() -> None:
    runner = FakeChatRunner()
    app = FirstCoderApp(command_handler=UnhandledCommandHandler(), chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/unknown")
        await pilot.press("enter")

    assert runner.inputs == []


@pytest.mark.anyio
async def test_firstcoder_app_displays_session_id_and_runner_display_lines() -> None:
    runner = FakeDisplayChatRunner()
    app = FirstCoderApp(chat_runner=runner, current_session=FakeSession())

    async with app.run_test() as pilot:
        assert app.sub_title == "Session: sess_test"
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
async def test_firstcoder_app_awaits_async_chat_runner_when_available() -> None:
    runner = FakeAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]
    assert runner.last_display_lines == ["async reply"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_streaming_tui_does_not_render_duplicate_final_markdown() -> None:
    runner = FakeStreamingTextAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        markdown_widgets = output.query("Markdown")
        assert len(markdown_widgets) == 1
    assert runner.inputs == ["hello"]


def test_firstcoder_app_installs_and_restores_stream_event_handler() -> None:
    runner = FakeStreamingAsyncChatRunner()
    original_handler = runner.stream_event_handler
    app = FirstCoderApp(chat_runner=runner)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="message_started"))
    app._restore_stream_event_handler(previous_handler)

    assert runner.seen == [ChatStreamEvent(kind="message_started")]
    assert runner.stream_event_handler is original_handler


def test_firstcoder_app_streams_text_delta_without_repeating_final_text(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    mounted_types = [type(widget).__name__ for widget in output.mounted]
    assert mounted_types == ["Markdown"]
    assert app._stream_text_buffer == "hello"
    assert runner.seen == [
        ChatStreamEvent(kind="text_delta", text="he"),
        ChatStreamEvent(kind="text_delta", text="llo"),
    ]


def test_firstcoder_app_streaming_skips_normalized_duplicate_assistant_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello\n"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["Markdown"]
    assert len(getattr(output.mounted[0], "updates")) == 1


def test_firstcoder_app_batches_stream_markdown_updates(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    app._append_stream_text("我")
    app._append_stream_text("在")
    app._append_stream_text("这里")

    markdown = output.mounted[0]
    assert getattr(markdown, "updates") == ["FirstCoder:\n\n我"]
    assert app._stream_text_buffer == "我在这里"

    app._flush_stream_text()

    assert getattr(markdown, "updates")[-1] == "FirstCoder:\n\n我在这里"


def test_firstcoder_app_records_streaming_assistant_text_in_transcript(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("你")
    app._append_stream_text("好")

    assistant_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.ASSISTANT]
    assert len(assistant_entries) == 1
    assert assistant_entries[0].body == "你好"


def test_firstcoder_app_shows_reasoning_delta_in_activity_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._append_reasoning_text("planning ")
    app._append_reasoning_text("tools")

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert output.mounted == []
    assert activity.updates == [
        "thinking [.  ] planning ",
        "thinking [.  ] planning tools",
    ]


def test_firstcoder_app_shows_working_indicator_without_reasoning_delta(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._show_working_indicator("planning next step...")
    app._complete_working_indicator()

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert output.mounted == []
    assert activity.updates == [
        "thinking [.  ] planning next step...",
        "streaming · response",
    ]


def test_firstcoder_app_animates_working_indicator(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._show_working_indicator("planning next step...")
    app._advance_working_animation()

    reasoning_entries = [entry for entry in app.transcript.entries if entry.kind == TuiEntryKind.REASONING]
    assert reasoning_entries == []
    assert activity.updates[-1] == "thinking [.. ] planning next step..."
    assert app._working_timer is timer

    app._complete_working_indicator()

    assert activity.updates[-1] == "streaming · response"
    assert timer.stopped is True
    assert app._working_timer is None


def test_firstcoder_app_streaming_final_response_skips_assistant_display_line(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello", "Tool result: echo success: ok"]
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="h"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    mounted_types = [type(widget).__name__ for widget in output.mounted]
    assert mounted_types.count("Markdown") == 1
    assert mounted_types == ["Markdown", "Static"]


def test_firstcoder_app_stream_event_handler_schedules_ui_updates_on_app_thread(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)
    calls: list[tuple[str, str]] = []
    scheduled: list[object] = []

    monkeypatch.setattr(app, "_append_stream_text", lambda text: calls.append(("text", text)))
    monkeypatch.setattr(app, "_append_stream_line", lambda label, text, include_label: calls.append((label, text)))
    monkeypatch.setattr(app, "_call_ui_thread", lambda callback, *args, **kwargs: scheduled.append((callback, args, kwargs)))

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert calls == []
    assert len(scheduled) == 2
    callback, args, kwargs = scheduled[1]
    callback(*args, **kwargs)
    assert calls == [("text", "hello")]


def test_firstcoder_app_tool_event_handler_schedules_live_status_on_app_thread(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)
    calls: list[str] = []
    scheduled: list[object] = []

    monkeypatch.setattr(app, "_write_line", lambda text, **kwargs: calls.append(text))
    monkeypatch.setattr(app, "_call_ui_thread", lambda callback, *args, **kwargs: scheduled.append((callback, args, kwargs)))

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_echo", name="echo", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert calls == []
    assert len(scheduled) == 3
    callback, args, kwargs = scheduled[2]
    callback(*args, **kwargs)
    assert calls == ["正在调用工具：echo"]


def test_firstcoder_app_updates_activity_line_for_tool_events(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=tool_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert activity.updates == ["running · echo", "thinking [.  ] reading echo result"]
    assert app._working_timer is timer

    app._advance_working_animation()

    assert activity.updates[-1] == "thinking [.. ] reading echo result"


def test_firstcoder_app_stops_post_tool_animation_when_next_tool_starts(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)})()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    first_call = ToolCall(id="call_echo", name="echo", arguments={})
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=first_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_ls", name="ls", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert timer.stopped is True
    assert app._working_timer is None
    assert activity.updates[-1] == "running · ls"


def test_firstcoder_app_updates_persistent_todo_panel_for_todo_events(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    todo_panel = FakeTodoPanel()
    app = FirstCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#todo-panel":
            return todo_panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=ToolCall(id="call_todo", name="todo", arguments={"action": "set"}),
            result=ToolResult(
                name="todo",
                ok=True,
                content="已设置任务清单",
                data={
                    "todos": [
                        {"id": "todo_1", "content": "读代码", "status": "done"},
                        {"id": "todo_2", "content": "跑测试", "status": "in_progress"},
                    ]
                },
            ),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert app.transcript.todos == [
        TuiTodoItem(id="todo_1", content="读代码", status="done"),
        TuiTodoItem(id="todo_2", content="跑测试", status="in_progress"),
    ]
    assert todo_panel.updates[-1] == "Todo\n[x] 读代码\n[~] 跑测试"


def test_firstcoder_app_updates_topbar_when_activity_changes(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    topbar = FakeTopbar()
    app = FirstCoderApp(current_session=FakeSession())
    monkeypatch.setattr(app, "is_mounted", True)
    monkeypatch.setattr(app, "_topbar_width", lambda: 120)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#topbar":
            return topbar
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._set_activity("waiting · permission")

    assert activity.updates == ["waiting · permission"]
    assert app._activity_text == "waiting · permission"
    assert "[#b28443]waiting · permission[/]" in topbar.updates[-1]


def test_firstcoder_app_live_tool_events_filter_final_tool_summary(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_echo", name="echo", arguments={})))
    runner.last_display_lines = [
        'Tool call: echo {"text": "hello"}',
        "Tool result: echo success: hello",
        "done",
    ]
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="done"))
    app._restore_tool_event_handler(previous_handler)

    rendered = "\n".join(output.lines)
    assert "正在调用工具：echo" in rendered
    assert "Tool call:" not in rendered
    assert "Tool result:" not in rendered
    assert [type(widget).__name__ for widget in output.mounted] == ["Static", "Markdown"]


def test_firstcoder_app_starts_new_stream_block_after_tool_event(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    runner.stream_event_handler = lambda event: None
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_stream_handler = app._install_stream_event_handler()
    previous_tool_handler = app._install_tool_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="我先看看。"))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="看完了。"))
    app._restore_tool_event_handler(previous_tool_handler)
    app._restore_stream_event_handler(previous_stream_handler)

    mounted_types = [type(widget).__name__ for widget in output.mounted]
    assert mounted_types == ["Markdown", "Static", "Markdown"]
    first_markdown, _, second_markdown = output.mounted
    assert getattr(first_markdown, "updates")[-1] == "FirstCoder:\n\n我先看看。"
    assert getattr(second_markdown, "updates")[-1] == "FirstCoder:\n\n看完了。"


def test_permission_requested_tool_event_uses_permission_style() -> None:
    event = ToolExecutionEvent(
        kind="permission_requested",
        tool_call=ToolCall(id="call_write", name="apply_patch", arguments={}),
    )

    assert _tool_event_entry_kind(event) == TuiEntryKind.PERMISSION
    assert _tool_event_status(event) == "permission_requested"
    assert _tool_event_label(event) == "permission requested"
    assert (
        _entry_classes(
            TuiTranscriptEntry(
                id=1,
                kind=TuiEntryKind.PERMISSION,
                body="apply_patch wants to edit firstcoder/app/tui.py",
                label="permission requested",
                status="permission_requested",
            )
        )
        == "message permission-message permission-requested"
    )


def test_tool_skipped_has_stable_gray_tool_class() -> None:
    assert (
        _entry_classes(
            TuiTranscriptEntry(
                id=1,
                kind=TuiEntryKind.TOOL,
                body="已暂停等待用户输入，跳过同批次后续工具调用。",
                label="tool shell skipped",
                status="skipped",
            )
        )
        == "message tool-message tool-skipped"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_firstcoder_app_displays_live_tool_status_during_turn() -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        text = "\n".join(
            str(getattr(widget, "content", getattr(widget, "renderable", "")))
            for widget in output.query("Static")
        )

    assert "正在调用工具：echo" in text
    assert "工具完成：echo：hello" in text
    assert runner.inputs == ["hello"]


def test_firstcoder_app_displays_pending_permission_prompt_immediately(monkeypatch) -> None:
    runner = FakePermissionWaitingRunner()
    output = FakeOutput()
    app = FirstCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="等待权限确认。"))

    rendered = "\n".join(
        [*output.lines, *(str(getattr(widget, "content", "")) for widget in output.mounted)]
    )
    assert "permission requested  write_path README.md" in rendered
    assert "写入文件需要用户确认。" in rendered
    assert "[1] deny" in rendered
    assert "[2] allow once" in rendered
    assert "[3] allow always" in rendered


@pytest.mark.anyio
async def test_firstcoder_app_displays_chat_errors_from_worker() -> None:
    runner = FailingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
async def test_firstcoder_app_rejects_chat_input_while_turn_is_running() -> None:
    runner = BlockingAsyncChatRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"first")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.click("#input")
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()
        runner.release.set()
        await pilot.pause()

    assert runner.inputs == ["first"]


@pytest.mark.anyio
async def test_firstcoder_app_routes_permission_answer_to_resume() -> None:
    runner = FakePermissionResumeRunner()
    app = FirstCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"allow once")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "allow_once")]
