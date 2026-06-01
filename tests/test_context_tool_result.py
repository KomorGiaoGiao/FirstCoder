from firstcoder.context.tool_result import normalize_tool_result
from firstcoder.tools.types import ToolResult


def test_normalize_tool_result_adds_metadata_for_context_part() -> None:
    result = ToolResult(
        name="shell",
        ok=True,
        content="pytest passed",
        data={"exit_code": 0},
    )

    normalized = normalize_tool_result(
        result,
        tool_call_id="call_1",
        task_hash="task_a",
        created_turn=3,
    )

    assert normalized.tool_name == "shell"
    assert normalized.tool_call_id == "call_1"
    assert normalized.ok is True
    assert normalized.content == "pytest passed"
    assert normalized.data == {"exit_code": 0}
    assert normalized.token_estimate > 0
    assert len(normalized.content_fingerprint) == 16
    assert normalized.compaction_state == "raw"
    assert normalized.task_hash == "task_a"
    assert normalized.created_turn == 3


def test_normalized_tool_result_can_be_stored_as_message_part() -> None:
    normalized = normalize_tool_result(
        ToolResult(name="read_file", ok=False, content="missing", error="not found"),
        tool_call_id="call_missing",
    )

    part = normalized.to_part(id="part_result", message_id="msg_tool")

    assert part.kind == "tool_result"
    assert part.content == "missing"
    assert part.metadata["tool_call_id"] == "call_missing"
    assert part.metadata["ok"] is False
    assert part.metadata["error"] == "not found"
    assert part.metadata["compaction_state"] == "raw"
