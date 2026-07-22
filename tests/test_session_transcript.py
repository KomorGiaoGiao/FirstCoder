from pathlib import Path

import pytest

from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.session.models import ShareOptions
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError
from firstcoder.session.transcript import TranscriptBuilder
from firstcoder.tools.types import ToolResult


def test_transcript_builder_keeps_conversation_order_and_redacts_text(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="demo")
    user_id = writer.append_user_message("读取 D:\\Project\\secret.txt TOKEN=abc")
    assistant_id = writer.append_assistant_response(ChatResponse(provider="fake", model="fake-model", content="我会处理 /home/user/project/file.txt"))

    transcript = TranscriptBuilder(store).build("sess_test")

    assert transcript.session.title == "demo"
    assert [entry.role for entry in transcript.entries] == ["user", "assistant"]
    assert transcript.entries[0].message_id == user_id
    assert transcript.entries[1].message_id == assistant_id
    assert "TOKEN=abc" not in transcript.entries[0].content
    assert "D:\\Project" not in transcript.entries[0].content
    assert "/home/user/project" not in transcript.entries[1].content


def test_transcript_builder_summarizes_tool_call_and_omits_tool_result_by_default(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="tools")
    writer.append_user_message("读文件")
    writer.append_assistant_response(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[ToolCall(id="call_1", name="view", arguments={"path": "README.md"})],
            finish_reason="tool_calls",
        )
    )
    writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="view", arguments={"path": "README.md"}),
        result=ToolResult(name="view", ok=True, content="README full content", data={"lines": 1}),
    )

    transcript = TranscriptBuilder(store).build("sess_test")

    tool_call = next(entry for entry in transcript.entries if entry.role == "tool_call")
    tool_result = next(entry for entry in transcript.entries if entry.role == "tool")
    assert tool_call.title == "Tool Call: view"
    assert '"path": "README.md"' in tool_call.content
    assert tool_result.content == "Status: success\nSummary: tool result omitted for sharing"
    assert "README full content" not in tool_result.content


def test_transcript_builder_can_include_bounded_tool_results(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="tools")
    writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="shell", arguments={}),
        result=ToolResult(name="shell", ok=False, content="line " * 200, error="failed"),
    )

    transcript = TranscriptBuilder(store).build(
        "sess_test",
        ShareOptions(include_tool_results=True, max_tool_result_chars=40),
    )

    tool_result = transcript.entries[0]
    assert tool_result.role == "tool"
    assert tool_result.content.startswith("Status: failed\n")
    assert len(tool_result.content) < 80
    assert tool_result.content.endswith("...")


def test_transcript_builder_uses_archive_placeholder_without_reading_archive_file(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    archive_dir = tmp_path / "archives" / "sess_test"
    archive_dir.mkdir(parents=True)
    (archive_dir / "ar_1.txt").write_text("raw archive secret TOKEN=abc", encoding="utf-8")
    store.append_event(
        SessionEvent(
            id="evt_tool",
            session_id="sess_test",
            type="tool_result",
            payload={
                "message_id": "msg_tool",
                "parts": [
                    {
                        "id": "part_tool",
                        "kind": "tool_result",
                        "content": "[Tool result archived]\narchive_id=ar_1\npreview=safe preview",
                        "metadata": {
                            "tool_name": "shell",
                            "ok": True,
                            "archive_id": "ar_1",
                            "summary": "shell 输出过大，已归档。",
                            "preview": "safe preview",
                        },
                    }
                ],
            },
        )
    )

    transcript = TranscriptBuilder(store).build("sess_test")

    assert transcript.entries[0].content == ("Status: success\n" "Archive: ar_1\n" "Summary: shell 输出过大，已归档。\n" "Preview: safe preview")
    assert "raw archive secret" not in transcript.entries[0].content


def test_transcript_builder_includes_checkpoint_summary(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_checkpoint",
            session_id="sess_test",
            type="checkpoint_created",
            payload={
                "id": "ckpt_1",
                "summary": "旧历史摘要",
                "tail_start_message_id": "msg_tail",
                "covered_until_message_id": "msg_old",
                "source_fingerprint": "fp",
            },
            created_at="2026-06-01T00:00:00Z",
        )
    )

    transcript = TranscriptBuilder(store).build("sess_test")

    assert transcript.entries[0].role == "checkpoint"
    assert transcript.entries[0].title == "Checkpoint: ckpt_1"
    assert transcript.entries[0].content == "旧历史摘要"


def test_transcript_builder_can_include_compaction_metadata(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_compact",
            session_id="sess_test",
            type="compaction_completed",
            payload={
                "trigger": "manual",
                "status": "success",
                "reason": "l2",
                "before_tokens": 1000,
                "after_tokens": 200,
            },
        )
    )

    hidden = TranscriptBuilder(store).build("sess_test")
    visible = TranscriptBuilder(store).build("sess_test", ShareOptions(include_compaction_metadata=True))

    assert hidden.entries == []
    assert visible.entries[0].role == "compaction"
    assert "manual" in visible.entries[0].content
    assert "1000 -> 200" in visible.entries[0].content


def test_transcript_builder_rejects_corrupt_session_with_session_error(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_corrupt.jsonl").write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        TranscriptBuilder(store).build("sess_corrupt")


def test_transcript_builder_rejects_empty_session_with_session_error(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_empty.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(SessionEmptyError):
        TranscriptBuilder(store).build("sess_empty")
