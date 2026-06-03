from dataclasses import dataclass, field

from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def test_current_session_state_proxies_replaced_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = AgentSession.create(store=store, session_id="sess_first", agents_md="")
    second = AgentSession.create(store=store, session_id="sess_second", agents_md="")
    state = CurrentSessionState(first)

    state.set_session(second)

    assert state.session_id == "sess_second"
    assert state.runtime_state is second.runtime_state
    assert state.rebuild_view().session_id == "sess_second"


def test_agent_chat_runner_uses_current_session_and_can_follow_resume(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = AgentSession.create(store=store, session_id="sess_first", agents_md="")
    second = AgentSession.create(store=store, session_id="sess_second", agents_md="")
    state = CurrentSessionState(first)
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="first reply"),
            ChatResponse(provider="fake", model="fake-model", content="second reply"),
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider)

    first_response = runner.run_user_turn("第一轮")
    state.set_session(second)
    second_response = runner.run_user_turn("第二轮")

    assert first_response.content == "first reply"
    assert second_response.content == "second reply"
    assert [message.parts[0].content for message in store.rebuild_session_view("sess_first").messages] == [
        "第一轮",
        "first reply",
    ]
    assert [message.parts[0].content for message in store.rebuild_session_view("sess_second").messages] == [
        "第二轮",
        "second reply",
    ]
