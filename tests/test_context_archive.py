import hashlib
import json

import pytest

from firstcoder.context.archive import ArchiveIntegrityError, ToolResultArchive
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.versions import ARCHIVE_SCHEMA_VERSION


def _part(content: str = "line\n" * 200) -> MessagePart:
    return MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content=content,
        metadata={"tool_name": "shell", "tool_call_id": "call_1"},
    )


def test_resume_projection_keeps_archive_placeholder(tmp_path) -> None:
    original = "very large output\n" * 200
    archive = ToolResultArchive(tmp_path)
    part = _part(original)
    archived = archive.make_placeholder(part, archive.store_original("sess_test", part))
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
            AgentMessage(id="msg_tool", session_id="sess_test", role="tool", parts=[archived]),
        ],
    )

    messages = ContextBuilder().build_provider_messages(view)
    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert messages[1].role == "tool"
    assert messages[1].tool_call_id == "call_1"
    assert "archive_id=" in messages[1].content
    assert original not in messages[1].content


def test_store_original_content_address_deduplicates(tmp_path) -> None:
    archive = ToolResultArchive(tmp_path)
    first = archive.store_original("sess_test", _part("same output"))
    second = archive.store_original("sess_test", _part("same output"))

    assert first == second
    assert first.archive_id == "ar_" + hashlib.sha256(b"same output").hexdigest()[:32]
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1


def test_store_original_uses_explicit_empty_content(tmp_path) -> None:
    part = _part("nonempty")
    record = ToolResultArchive(tmp_path).store_original("sess_test", part, original_content="")

    assert record.original_chars == 0
    assert (tmp_path / "archives" / "sess_test" / f"{record.archive_id}.txt").read_text() == ""


@pytest.mark.parametrize("session_id,archive_id", [("../escape", "ar_safe"), ("sess", "../escape")])
def test_archive_path_traversal_is_rejected(tmp_path, session_id, archive_id) -> None:
    with pytest.raises(ValueError):
        archive = ToolResultArchive(tmp_path)
        if session_id == "../escape":
            archive.store_original(session_id, _part())
        else:
            archive.read(session_id, archive_id)


def test_preexisting_content_addressed_text_with_other_content_is_integrity_error(tmp_path) -> None:
    content = "expected"
    archive_id = "ar_" + hashlib.sha256(content.encode()).hexdigest()[:32]
    path = tmp_path / "archives" / "sess_test" / f"{archive_id}.txt"
    path.parent.mkdir(parents=True)
    path.write_text("wrong", encoding="utf-8")

    with pytest.raises(ArchiveIntegrityError):
        ToolResultArchive(tmp_path).store_original("sess_test", _part(content))


def test_each_session_has_its_own_content_addressed_files(tmp_path) -> None:
    archive = ToolResultArchive(tmp_path)
    record_one = archive.store_original("first", _part("same output"))
    record_two = archive.store_original("second", _part("same output"))

    assert record_one.archive_id == record_two.archive_id
    assert (tmp_path / "archives" / "first" / f"{record_one.archive_id}.txt").exists()
    assert (tmp_path / "archives" / "second" / f"{record_two.archive_id}.txt").exists()


def test_v2_placeholder_has_no_raw_preview_and_is_bounded(tmp_path) -> None:
    raw = "SECRET_RESULT_SHOULD_NOT_APPEAR " * 100
    archive = ToolResultArchive(tmp_path)
    record = archive.store_original("sess_test", _part(raw))
    placeholder = archive.make_placeholder(_part(raw), record, summary="x" * 600, key_errors=("first", "second", "third", "fourth"))

    assert len(placeholder.content) <= 480
    assert raw not in placeholder.content
    assert "SECRET_RESULT_SHOULD_NOT_APPEAR" not in placeholder.content
    assert "preview" not in placeholder.content
    assert "preview" not in placeholder.metadata
    assert placeholder.metadata["compacted_by"] == "l3_archive"
    assert placeholder.content.endswith("Use retrieve_archive(archive_id, ...) to inspect the original.")


def test_v2_placeholder_retains_required_details_inside_limit(tmp_path) -> None:
    part = _part("source that must not be repeated")
    part.metadata.update({"ok": False, "tool_name": "run_command"})
    archive = ToolResultArchive(tmp_path)
    record = archive.store_original("sess_test", part)

    placeholder = archive.make_placeholder(
        part,
        record,
        lifecycle="superseded",
        summary="summary",
        key_errors=("first", "second", "third", "ignored"),
    )

    assert len(placeholder.content) <= 480
    assert f"archive_id={record.archive_id}" in placeholder.content
    assert "tool=run_command" in placeholder.content
    assert "status=failed" in placeholder.content
    assert "lifecycle=superseded" in placeholder.content
    assert f"original_tokens={record.original_tokens}" in placeholder.content
    assert "summary=summary" in placeholder.content
    assert placeholder.content.count("key_errors=") == 3
    assert "key_errors=first" in placeholder.content
    assert placeholder.content.endswith("Use retrieve_archive(archive_id, ...) to inspect the original.")


@pytest.mark.parametrize("metadata", [{"status": "error"}, {"status": "failed"}, {"is_error": True}])
def test_v2_placeholder_normalizes_failure_signals(tmp_path, metadata) -> None:
    part = _part("result")
    part.metadata.update(metadata)
    archive = ToolResultArchive(tmp_path)
    record = archive.store_original("sess_test", part)

    placeholder = archive.make_placeholder(part, record)

    assert "status=failed" in placeholder.content


def test_v2_placeholder_normalizes_unknown_status_to_success(tmp_path) -> None:
    part = _part("result")
    part.metadata["status"] = "completed"
    archive = ToolResultArchive(tmp_path)
    record = archive.store_original("sess_test", part)

    assert "status=success" in archive.make_placeholder(part, record).content


def test_v2_read_rejects_non_content_addressed_id(tmp_path) -> None:
    path = tmp_path / "archives" / "sess_test"
    path.mkdir(parents=True)
    raw = "content"
    (path / "ar_notcontentaddressed.txt").write_text(raw, encoding="utf-8")
    (path / "ar_notcontentaddressed.json").write_text(
        json.dumps(
            {
                "archive_id": "ar_notcontentaddressed",
                "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "original_chars": len(raw),
                "original_tokens": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "schema_version": ARCHIVE_SCHEMA_VERSION,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ArchiveIntegrityError, match="archive id"):
        ToolResultArchive(tmp_path).read("sess_test", "ar_notcontentaddressed")
