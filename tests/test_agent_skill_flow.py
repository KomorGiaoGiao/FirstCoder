from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


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


def test_high_confidence_project_skill_loads_before_first_provider_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _write_info_database_like_project(tmp_path)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(store=store, session_id="sess_skill", project_root=tmp_path)
    provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("按框架跑一次今天的全球家办资讯简报")

    events = store.list_events("sess_skill")
    skill_events = [event for event in events if event.type.startswith("skill_")]
    assert [event.type for event in skill_events] == [
        "skill_selected",
        "skill_loaded",
        "skill_required_file_loaded",
    ]
    assert skill_events[0].payload["skill_path"] == "skills/global-family-office-news-brief.md"
    assert skill_events[1].payload["skill_path"] == "skills/global-family-office-news-brief.md"
    assert skill_events[2].payload["file_path"] == "docs/evidence-policy.md"
    system_prompt = provider.requests[0].messages[0].content
    assert "# 全球家族办公室资讯简报" in system_prompt
    assert "Required files: docs/evidence-policy.md" in system_prompt
    assert "# Evidence Policy" in system_prompt
    assert "# 敏感 Claim 复核" not in system_prompt


def test_high_confidence_global_skill_loads_before_first_provider_call(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    global_skill_dir = home / ".agents" / "skills" / "fetch-tweet"
    global_skill_dir.mkdir(parents=True)
    (global_skill_dir / "SKILL.md").write_text(
        "---\nname: fetch-tweet\ndescription: Fetch X/Twitter posts.\n---\n\n# Fetch Tweet\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(store=store, session_id="sess_global_skill", project_root=tmp_path)
    provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("帮我读取这个 x.com 帖子内容")

    skill_events = [event for event in store.list_events("sess_global_skill") if event.type.startswith("skill_")]
    assert [event.type for event in skill_events] == ["skill_selected", "skill_loaded"]
    assert skill_events[1].payload["skill_scope"] == "global"
    assert skill_events[1].payload["skill_path"] == "fetch-tweet/SKILL.md"
    system_prompt = provider.requests[0].messages[0].content
    assert "# Fetch Tweet" in system_prompt
    assert f"root={home / '.agents' / 'skills'}" in system_prompt


def test_ambiguous_skill_route_does_not_auto_load(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "source-verification.md").write_text("# Source Verification\n\n来源核验。", encoding="utf-8")
    (skills_dir / "second-hop-verification.md").write_text("# Second Hop\n\n二跳追证 来源核验。", encoding="utf-8")
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(store=store, session_id="sess_ambiguous", project_root=tmp_path)
    provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("帮我做来源核验")

    skill_events = [event for event in store.list_events("sess_ambiguous") if event.type.startswith("skill_")]
    assert skill_events == []
    system_prompt = provider.requests[0].messages[0].content
    assert "# Source Verification" not in system_prompt
    assert "# Second Hop" not in system_prompt


def test_resume_reloads_previously_loaded_skill_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    _write_info_database_like_project(tmp_path)
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    original = AgentSession.from_project(store=store, session_id="sess_resume_skill", project_root=tmp_path)
    provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    AgentLoop(session=original, provider=provider).run_user_turn("按框架跑一次今天的全球家办资讯简报")

    resumed = AgentSession.resume(
        store=store,
        session_id="sess_resume_skill",
        agents_md=(tmp_path / "AGENTS.md").read_text(encoding="utf-8"),
        skill_catalog=original.skill_catalog,
    )
    resumed_provider = RecordingProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=resumed, provider=resumed_provider).run_user_turn("继续")

    system_prompt = resumed_provider.requests[1].messages[0].content
    assert "# 全球家族办公室资讯简报" in system_prompt
    assert "# Evidence Policy" in system_prompt


def _write_info_database_like_project(root: Path) -> None:
    (root / "AGENTS.md").write_text(
        "| 用户意图 | 优先读取 |\n" "|---|---|\n" "| “今天/某天全球家办有什么新闻”“帮我找资讯并总结” | `skills/global-family-office-news-brief.md` |\n",
        encoding="utf-8",
    )
    skills_dir = root / "skills"
    skills_dir.mkdir()
    (skills_dir / "INDEX.md").write_text(
        "| Skill | 触发场景 |\n|---|---|\n| `global-family-office-news-brief.md` | 全球家办资讯 |\n",
        encoding="utf-8",
    )
    (skills_dir / "global-family-office-news-brief.md").write_text(
        "# 全球家族办公室资讯简报\n\n开始前必须读取 `docs/evidence-policy.md`。\n",
        encoding="utf-8",
    )
    (skills_dir / "sensitive-claim-review.md").write_text("# 敏感 Claim 复核\n", encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "evidence-policy.md").write_text("# Evidence Policy\n\nUse primary sources.\n", encoding="utf-8")
