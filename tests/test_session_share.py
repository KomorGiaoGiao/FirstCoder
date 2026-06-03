from pathlib import Path

import pytest

from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.session.models import ShareOptions
from firstcoder.session.errors import SessionCorruptError
from firstcoder.session.share import SessionShareService
from firstcoder.tools.types import ToolResult


def test_share_service_exports_markdown_to_default_path(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="分享测试", workspace="D:\\Project")
    writer.append_user_message("你好 TOKEN=abc")
    writer.append_assistant_response(ChatResponse(provider="fake", model="fake-model", content="收到"))

    path = SessionShareService(store).export_markdown("sess_test")

    assert path == tmp_path / "shares" / "sess_test.md"
    content = path.read_text(encoding="utf-8")
    assert "# 分享测试" in content
    assert "- Session: sess_test" in content
    assert "- Workspace: [REDACTED_PATH]" in content
    assert "- Model: fake/fake-model" in content
    assert "TOKEN=abc" not in content
    assert "TOKEN=[REDACTED_SECRET]" in content
    assert "## Conversation" in content
    assert "### User" in content
    assert "### Assistant" in content


def test_share_service_redacts_title(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="修复 TOKEN=abc D:\\Project\\secret.txt")

    path = SessionShareService(store).export_markdown("sess_test")
    content = path.read_text(encoding="utf-8")

    assert content.startswith("# 修复 TOKEN=[REDACTED_SECRET] [REDACTED_PATH]")
    assert "TOKEN=abc" not in content
    assert "D:\\Project" not in content


def test_share_service_does_not_export_full_tool_result_by_default(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="tools")
    writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="shell", arguments={}),
        result=ToolResult(name="shell", ok=True, content="secret output TOKEN=abc"),
    )

    path = SessionShareService(store).export_markdown("sess_test")
    content = path.read_text(encoding="utf-8")

    assert "secret output" not in content
    assert "tool result omitted for sharing" in content


def test_share_service_can_include_bounded_tool_results(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="tools")
    writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="shell", arguments={}),
        result=ToolResult(name="shell", ok=True, content="line " * 200),
    )

    path = SessionShareService(store).export_markdown(
        "sess_test",
        options=ShareOptions(include_tool_results=True, max_tool_result_chars=30),
    )
    content = path.read_text(encoding="utf-8")

    assert "line line line line line li..." in content
    assert "line " * 20 not in content


def test_share_service_does_not_read_archive_raw_content(tmp_path: Path) -> None:
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
                        "content": "[Tool result archived]\narchive_id=ar_1",
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

    path = SessionShareService(store).export_markdown("sess_test")
    content = path.read_text(encoding="utf-8")

    assert "Archive: ar_1" in content
    assert "safe preview" in content
    assert "raw archive secret" not in content


def test_share_service_exports_to_custom_path_and_overwrites(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="custom")
    output = tmp_path / "custom" / "out.md"

    service = SessionShareService(store)
    first = service.export_markdown("sess_test", output_path=output)
    output.write_text("old", encoding="utf-8")
    second = service.export_markdown("sess_test", output_path=output)

    assert first == output
    assert second == output
    assert output.read_text(encoding="utf-8").startswith("# custom")


def test_share_service_can_include_event_ids(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="ids")
    writer.append_user_message("hello")

    path = SessionShareService(store).export_markdown("sess_test", options=ShareOptions(include_event_ids=True))
    content = path.read_text(encoding="utf-8")

    assert "Event:" in content


def test_share_service_propagates_session_level_errors_for_corrupt_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    (tmp_path / "sessions" / "sess_corrupt.jsonl").write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        SessionShareService(store).export_markdown("sess_corrupt")
