from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall


@dataclass
class RecordingProvider(ChatProvider):
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            basis_message_id = next(message.content.split("basis_message_id=", 1)[1].split("]", 1)[0] for message in reversed(request.messages) if "basis_message_id=" in message.content)
            return ChatResponse(provider=self.name, model=self.model, content=f'{{"decision":"uncertain","basis_message_id":"{basis_message_id}"}}')
        return self.responses.pop(0)


def test_user_language_does_not_auto_load_skill_before_provider_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _write_skill_project(tmp_path)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(store=store, session_id="sess_skill", project_root=tmp_path)
    provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("按框架跑一次今天的全球家办资讯简报")

    assert [event for event in store.list_events("sess_skill") if event.type.startswith("skill_")] == []
    system_prompt = provider.requests[0].messages[0].content
    assert "- global-family-office-news-brief: 全球家办资讯简报" in system_prompt
    assert "skills/global-family-office-news-brief.md" not in system_prompt
    assert "# 全球家族办公室资讯简报" not in system_prompt


def test_model_load_skill_call_returns_body_in_next_provider_request_and_audits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _write_skill_project(tmp_path)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(store=store, session_id="sess_skill", project_root=tmp_path)
    provider = RecordingProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_skill", name="load_skill", arguments={"name": "global-family-office-news-brief", "args": "today"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )

    AgentLoop(session=session, provider=provider).run_user_turn("生成今天的简报")

    events = store.list_events("sess_skill")
    assert [event.type for event in events if event.type.startswith("skill_")] == ["skill_selected", "skill_loaded"]
    tool_messages = [message for message in provider.requests[-1].messages if message.role == "tool"]
    assert len(tool_messages) == 1
    assert "Loaded skill: global-family-office-news-brief" in tool_messages[0].content
    assert "Arguments: today" in tool_messages[0].content
    assert "# 全球家族办公室资讯简报" in tool_messages[0].content
    assert "# Evidence Policy" not in tool_messages[0].content


def test_resume_replays_loaded_skill_tool_result_without_reading_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _write_skill_project(tmp_path)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(store=store, session_id="sess_resume", project_root=tmp_path)
    provider = RecordingProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_skill", name="load_skill", arguments={"name": "global-family-office-news-brief"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    AgentLoop(session=original, provider=provider).run_user_turn("生成简报")
    (tmp_path / "skills" / "global-family-office-news-brief.md").unlink()

    resumed = AgentSession.resume(
        store=store,
        session_id="sess_resume",
        agents_md=(tmp_path / "AGENTS.md").read_text(encoding="utf-8"),
        skill_catalog=original.skill_catalog,
    )
    resumed_provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="继续完成")])

    AgentLoop(session=resumed, provider=resumed_provider).run_user_turn("继续")

    request_text = "\n".join(message.content for request in resumed_provider.requests for message in request.messages)
    assert "Loaded skill: global-family-office-news-brief" in request_text
    assert "# 全球家族办公室资讯简报" in request_text


def _write_skill_project(root: Path) -> None:
    (root / "AGENTS.md").write_text("项目规则。\n", encoding="utf-8")
    skills_dir = root / "skills"
    skills_dir.mkdir()
    (skills_dir / "global-family-office-news-brief.md").write_text(
        "---\nname: global-family-office-news-brief\ndescription: 全球家办资讯简报\n---\n\n# 全球家族办公室资讯简报\n\n开始前必须读取 `docs/evidence-policy.md`。\n",
        encoding="utf-8",
    )
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "evidence-policy.md").write_text("# Evidence Policy\n", encoding="utf-8")
