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

    def run_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        return ChatResponse(provider="fake", model="fake", content=f"reply:{content}")


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
