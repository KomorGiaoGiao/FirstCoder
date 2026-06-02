from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState


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
