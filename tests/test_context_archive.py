import json

from firstcoder.context.archive import ToolResultArchive
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView


def test_large_tool_result_is_written_to_archive(tmp_path) -> None:
    part = MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content="line\n" * 200,
        metadata={"tool_name": "shell", "tool_call_id": "call_1"},
    )

    archived = ToolResultArchive(tmp_path).archive_part(session_id="sess_test", part=part)

    archive_id = archived.metadata["archive_id"]
    archive_dir = tmp_path / ".firstcoder" / "archives" / "sess_test"
    assert (archive_dir / f"{archive_id}.txt").read_text(encoding="utf-8") == "line\n" * 200

    metadata = json.loads((archive_dir / f"{archive_id}.json").read_text(encoding="utf-8"))
    assert metadata["archive_id"] == archive_id
    assert metadata["session_id"] == "sess_test"
    assert metadata["part_id"] == "part_result"
    assert metadata["tool_call_id"] == "call_1"
    assert metadata["original_tokens"] > metadata["preview_tokens"]


def test_archive_placeholder_keeps_archive_id_summary_and_preview(tmp_path) -> None:
    content = "abcdef" * 200
    part = MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content=content,
        metadata={"tool_name": "read_file", "tool_call_id": "call_1"},
    )

    archived = ToolResultArchive(tmp_path, preview_chars=24).archive_part(
        session_id="sess_test",
        part=part,
        summary="read_file 输出过大，已归档。",
    )

    assert archived.kind == "tool_result"
    assert archived.content.startswith("[Tool result archived]")
    assert "archive_id=" in archived.content
    assert "preview=abcdefabcdefabcdefabcdef" in archived.content
    assert archived.metadata["summary"] == "read_file 输出过大，已归档。"
    assert archived.metadata["preview"] == "abcdefabcdefabcdefabcdef"
    assert archived.metadata["original_tokens"] > archived.metadata["preview_tokens"]
    assert archived.metadata["compaction_state"] == "archived"
    assert archived.metadata["archive_path"].endswith(".txt")


def test_archived_tool_result_is_not_archived_twice(tmp_path) -> None:
    part = MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content="large result" * 200,
        metadata={"tool_name": "shell", "tool_call_id": "call_1"},
    )
    archive = ToolResultArchive(tmp_path)

    first = archive.archive_part(session_id="sess_test", part=part)
    second = archive.archive_part(session_id="sess_test", part=first)

    assert second == first
    archive_dir = tmp_path / ".firstcoder" / "archives" / "sess_test"
    assert len(list(archive_dir.glob("*.txt"))) == 1
    assert len(list(archive_dir.glob("*.json"))) == 1


def test_archive_part_accepts_caller_provided_archive_id(tmp_path) -> None:
    part = MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content="large result" * 200,
        metadata={"tool_name": "shell", "tool_call_id": "call_1"},
    )

    archived = ToolResultArchive(tmp_path).archive_part(
        session_id="sess_test",
        part=part,
        archive_id="ar_existing",
    )

    archive_dir = tmp_path / ".firstcoder" / "archives" / "sess_test"
    assert archived.metadata["archive_id"] == "ar_existing"
    assert (archive_dir / "ar_existing.txt").exists()
    assert (archive_dir / "ar_existing.json").exists()


def test_resume_projection_keeps_archive_placeholder(tmp_path) -> None:
    original = "very large output\n" * 200
    archived = ToolResultArchive(tmp_path, preview_chars=20).archive_part(
        session_id="sess_test",
        part=MessagePart(
            id="part_result",
            message_id="msg_tool",
            kind="tool_result",
            content=original,
            metadata={"tool_name": "shell", "tool_call_id": "call_1"},
        ),
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_call",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={"tool_name": "shell", "tool_call_id": "call_1", "arguments": {}},
                    )
                ],
            ),
            AgentMessage(
                id="msg_tool",
                session_id="sess_test",
                role="tool",
                parts=[archived],
            )
        ],
    )

    messages = ContextBuilder().build_provider_messages(view)

    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert messages[1].role == "tool"
    assert messages[1].tool_call_id == "call_1"
    assert "archive_id=" in messages[1].content
    assert original not in messages[1].content
