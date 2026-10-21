"""provider cassette 录制/回放基础设施测试。

inner 用本地 FakeProvider，不触网、不需要 API key。anyio 写法对齐
``tests/test_agent_context_loop.py`` 等现有文件（anyio pytest 插件随 anyio 自动注册）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from firstcoder.providers.base import ChatProvider
from firstcoder.providers.cassette import (
    CassetteMismatchError,
    CassetteMissingError,
    CassetteNameError,
    CassetteProvider,
    CassetteUnusedError,
    resolve_cassette_path,
)
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderDiagnostics,
    TokenUsage,
    ToolCall,
)


@dataclass
class FakeProvider(ChatProvider):
    """参照 tests/test_agent_e2e.py 的 per-file FakeProvider 模式。"""

    responses: list[ChatResponse] = field(default_factory=list)
    stream_events: list[ChatStreamEvent] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider 没有剩余响应")
        return self.responses.pop(0)

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        self.requests.append(request)
        events = self.stream_events

        async def stream() -> AsyncIterator[ChatStreamEvent]:
            for event in events:
                yield event

        return stream()


def _sample_request(content: str = "你好") -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content=content)])


def _sample_response(raw: object = None) -> ChatResponse:
    return ChatResponse(
        provider="fake",
        model="fake-model",
        content="收到",
        tool_calls=[ToolCall(id="call_1", name="view", arguments={"path": "README.md"})],
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        diagnostics=ProviderDiagnostics(reasoning="因为", raw_finish_reason="stop", warnings=["w"]),
        raw=raw,
    )


def _sample_stream_events() -> list[ChatStreamEvent]:
    return [
        ChatStreamEvent(kind="message_started"),
        ChatStreamEvent(kind="text_delta", text="收到"),
        ChatStreamEvent(
            kind="message_completed",
            response=_sample_response(),
            diagnostics=ProviderDiagnostics(reasoning="流式诊断"),
        ),
    ]


def test_record_then_replay_complete_roundtrip(tmp_path) -> None:
    path = tmp_path / "roundtrip.json"
    inner = FakeProvider(responses=[_sample_response(raw={"sdk": "object"})])
    provider = CassetteProvider(inner=inner, path=path, mode="record")
    recorded = provider.complete(_sample_request())
    provider.close()

    assert path.exists()
    replayed = CassetteProvider(inner=None, path=path, mode="replay").complete(_sample_request())

    # raw 是 SDK 原始对象, 录制时丢弃; 其余字段逐字段相等
    assert recorded.raw == {"sdk": "object"}
    assert replayed.raw is None
    assert replayed == _sample_response(raw=None)
    assert replayed.tool_calls[0].arguments == {"path": "README.md"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_record_then_replay_roundtrip(tmp_path) -> None:
    path = tmp_path / "stream.json"
    inner = FakeProvider(stream_events=_sample_stream_events())
    provider = CassetteProvider(inner=inner, path=path, mode="record")
    recorded_events = [event async for event in provider.astream(_sample_request())]
    provider.close()

    replayer = CassetteProvider(inner=None, path=path, mode="replay")
    replayed_events = [event async for event in replayer.astream(_sample_request())]
    replayer.close()

    assert [event.kind for event in replayed_events] == ["message_started", "text_delta", "message_completed"]
    assert replayed_events == [event for event in recorded_events]
    completed = replayed_events[-1]
    assert completed.response is not None
    assert completed.response.content == "收到"
    assert completed.response.raw is None


def test_replay_request_mismatch_raises_with_diff(tmp_path) -> None:
    path = tmp_path / "mismatch.json"
    provider = CassetteProvider(inner=FakeProvider(responses=[_sample_response()]), path=path, mode="record")
    provider.complete(_sample_request("原始问题"))
    provider.close()

    replayer = CassetteProvider(inner=None, path=path, mode="replay")
    with pytest.raises(CassetteMismatchError) as excinfo:
        replayer.complete(_sample_request("改了的问题"))

    message = str(excinfo.value)
    assert "messages" in message
    assert "原始问题" in message
    assert "改了的问题" in message


def test_replay_unconsumed_interaction_raises_on_close(tmp_path) -> None:
    path = tmp_path / "unused.json"
    provider = CassetteProvider(inner=FakeProvider(responses=[_sample_response(), _sample_response()]), path=path, mode="record")
    provider.complete(_sample_request("第一问"))
    provider.complete(_sample_request("第二问"))
    provider.close()

    replayer = CassetteProvider(inner=None, path=path, mode="replay")
    replayer.complete(_sample_request("第一问"))
    with pytest.raises(CassetteUnusedError, match="1 条未消费"):
        replayer.close()


def test_ci_auto_mode_missing_cassette_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(CassetteMissingError, match="cassette 不存在"):
        CassetteProvider(inner=None, path=tmp_path / "missing.json", mode="auto")


def test_api_key_redacted_before_save(tmp_path) -> None:
    path = tmp_path / "redact.json"
    api_key = "sk-test1234567890abcdefghijklmnop"
    inner = FakeProvider(responses=[ChatResponse(provider="fake", model="fake-model", content=f"key is {api_key}")])
    provider = CassetteProvider(inner=inner, path=path, mode="record")
    provider.complete(_sample_request())
    provider.close()

    text = path.read_text(encoding="utf-8")
    assert api_key not in text
    assert "[REDACTED_API_KEY]" in text


def test_cassette_name_path_traversal_rejected(tmp_path) -> None:
    for bad_name in ("../evil", "a/../../evil", "C:/evil/x", "/abs/evil"):
        with pytest.raises(CassetteNameError):
            resolve_cassette_path(tmp_path, bad_name)
    # 合法的 provider/scenario 式子路径
    assert resolve_cassette_path(tmp_path, "fake/scenario") == tmp_path / "fake" / "scenario.json"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_acomplete_replays(tmp_path) -> None:
    path = tmp_path / "acomplete.json"
    provider = CassetteProvider(inner=FakeProvider(responses=[_sample_response()]), path=path, mode="record")
    await provider.acomplete(_sample_request())
    provider.close()

    replayer = CassetteProvider(inner=None, path=path, mode="replay")
    replayed = await replayer.acomplete(_sample_request())
    replayer.close()

    assert replayed == _sample_response(raw=None)


def test_committed_example_cassette_replays_without_inner(cassette_provider) -> None:
    """提交在 tests/fixtures/cassettes/ 的示例 cassette：回放完全不需要 inner。"""

    provider = cassette_provider("fake/example_complete", inner=None, mode="replay")
    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="示例问题")]))

    assert response.provider == "fake"
    assert response.model == "fake-model"
    assert response.content == "示例回复"
    assert response.raw is None
