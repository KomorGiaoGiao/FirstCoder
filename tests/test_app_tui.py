import pytest

from firstcoder.app.commands import CommandResult
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.providers.types import ChatResponse


class FakeSession:
    session_id = "sess_test"
    runtime_state = SessionRuntimeState(session_id="sess_test")

    def rebuild_view(self) -> SessionView:
        return SessionView(session_id="sess_test")


def test_firstcoder_app_can_be_created_with_command_handler() -> None:
    handler = ContextCommandHandler(session=FakeSession())

    app = FirstCoderApp(command_handler=handler, config=FirstCoderTuiConfig(title="TestCoder"))

    assert app.command_handler is handler
    assert app.config.title == "TestCoder"


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
