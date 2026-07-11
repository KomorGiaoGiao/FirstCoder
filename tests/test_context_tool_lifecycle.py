"""Pure tool-result lifecycle index tests."""

from __future__ import annotations

from firstcoder.context.models import AgentMessage, MessagePart
from firstcoder.context.tool_lifecycle import ToolResultLifecycle, index_tool_result_lifecycles


def _call(call_id: str, name: str, arguments: dict[str, object] | None = None) -> AgentMessage:
    return AgentMessage(
        id=f"assistant-{call_id}",
        session_id="session",
        role="assistant",
        parts=[
            MessagePart(
                id=f"call-{call_id}",
                message_id=f"assistant-{call_id}",
                kind="tool_call",
                content="",
                metadata={"tool_call_id": call_id, "tool_name": name, "arguments": arguments or {}},
            )
        ],
    )


def _result(
    call_id: str,
    name: str,
    *,
    content: str = "output",
    ok: bool = True,
    data: dict[str, object] | None = None,
) -> AgentMessage:
    message_id = f"tool-{call_id}"
    return AgentMessage(
        id=message_id,
        session_id="session",
        role="tool",
        parts=[
            MessagePart(
                id=f"result-{call_id}",
                message_id=message_id,
                kind="tool_result",
                content=content,
                metadata={"tool_call_id": call_id, "tool_name": name, "ok": ok, "data": data or {}},
            )
        ],
    )


def _view(call_id: str, *, offset: int = 0, limit: int = 200, truncated: bool = False) -> list[AgentMessage]:
    return [
        _call(call_id, "view", {"path": "a.py", "offset": offset, "limit": limit}),
        _result(
            call_id,
            "view",
            data={
                "path": "a.py",
                "start_line": offset + 1,
                "end_line": offset + limit,
                "total_lines": 400,
                "truncated": truncated,
            },
        ),
    ]


def _lifecycle(messages: list[AgentMessage], call_id: str) -> ToolResultLifecycle:
    return index_tool_result_lifecycles(messages)[(f"tool-{call_id}", f"result-{call_id}")].lifecycle


def test_view_then_edit_marks_read_stale():
    messages = _view("read") + [
        _call("edit", "edit", {"path": "a.py"}),
        _result("edit", "edit", data={"path": "a.py"}),
    ]

    assert _lifecycle(messages, "read") is ToolResultLifecycle.STALE


def test_failed_edit_does_not_mark_read_stale():
    messages = _view("read") + [
        _call("edit", "edit", {"path": "a.py"}),
        _result("edit", "edit", ok=False, data={"path": "a.py"}),
    ]

    assert _lifecycle(messages, "read") is ToolResultLifecycle.FRESH


def test_empty_view_range_is_fresh_but_not_a_source_target():
    messages = [
        _call("empty", "view", {"path": "a.py", "offset": 400, "limit": 20}),
        _result(
            "empty",
            "view",
            data={"path": "a.py", "start_line": None, "end_line": None, "total_lines": 20, "truncated": False},
        ),
    ] + _view("reread", offset=0, limit=20, truncated=False)

    record = index_tool_result_lifecycles(messages)[("tool-empty", "result-empty")]

    assert record.lifecycle is ToolResultLifecycle.FRESH
    assert record.source_targets == ()


def test_view_then_covering_view_marks_first_read_superseded():
    messages = _view("first", offset=0, limit=20, truncated=True) + _view("second", offset=0, limit=40, truncated=True)

    assert _lifecycle(messages, "first") is ToolResultLifecycle.SUPERSEDED
    assert _lifecycle(messages, "second") is ToolResultLifecycle.FRESH


def test_reread_after_edit_prioritizes_superseded_over_stale():
    messages = _view("first", offset=0, limit=20, truncated=True) + [
        _call("edit", "edit", {"path": "a.py"}),
        _result("edit", "edit", data={"path": "a.py"}),
    ] + _view("second", offset=0, limit=20, truncated=True)

    assert _lifecycle(messages, "first") is ToolResultLifecycle.SUPERSEDED


def test_non_overlapping_view_ranges_stay_fresh():
    messages = _view("first", offset=0, limit=20, truncated=True) + _view("second", offset=40, limit=20, truncated=True)

    assert _lifecycle(messages, "first") is ToolResultLifecycle.FRESH
    assert _lifecycle(messages, "second") is ToolResultLifecycle.FRESH


def test_truncated_read_multi_is_not_a_superseding_source_read():
    messages = [
        _call("multi", "read_multi", {"paths": ["a.py"]}),
        _result("multi", "read_multi", data={"files": [{"path": "a.py"}], "truncated": True}),
    ] + _view("view", offset=0, limit=200, truncated=False)

    assert _lifecycle(messages, "multi") is ToolResultLifecycle.FRESH


def test_complete_view_supersedes_full_file_read_multi():
    messages = [
        _call("multi", "read_multi", {"paths": ["a.py"]}),
        _result("multi", "read_multi", data={"files": [{"path": "a.py"}], "truncated": False}),
    ] + _view("view", offset=0, limit=400, truncated=False)

    assert _lifecycle(messages, "multi") is ToolResultLifecycle.SUPERSEDED
    assert _lifecycle(messages, "view") is ToolResultLifecycle.FRESH


def test_unknown_shell_output_does_not_make_source_read_stale():
    messages = _view("read", offset=0, limit=20, truncated=True) + [
        _call("shell", "shell", {"command": "cat a.py"}),
        _result("shell", "shell", content="changed a.py"),
    ]

    assert _lifecycle(messages, "read") is ToolResultLifecycle.FRESH
    assert _lifecycle(messages, "shell") is ToolResultLifecycle.DERIVED


def test_write_and_delete_mark_prior_reads_stale():
    messages = _view("written", offset=0, limit=20, truncated=True) + [
        _call("write", "write", {"path": "a.py"}),
        _result("write", "write", data={"path": "a.py"}),
    ] + _view("deleted", offset=40, limit=20, truncated=True) + [
        _call("delete", "delete", {"path": "a.py"}),
        _result("delete", "delete", data={"path": "a.py"}),
    ]

    assert _lifecycle(messages, "written") is ToolResultLifecycle.STALE
    assert _lifecycle(messages, "deleted") is ToolResultLifecycle.STALE


def test_apply_patch_changed_and_move_paths_mark_reads_stale():
    messages = _view("changed", offset=0, limit=20, truncated=True) + _view("moved", offset=40, limit=20, truncated=True) + [
        _call("patch", "apply_patch", {"patch": "..."}),
        _result(
            "patch",
            "apply_patch",
            data={
                "changed_files": ["a.py"],
                "deleted_files": [],
                "created_files": [],
                "moved_files": [{"source": "a.py", "destination": "b.py"}],
            },
        ),
    ]

    assert _lifecycle(messages, "changed") is ToolResultLifecycle.STALE
    assert _lifecycle(messages, "moved") is ToolResultLifecycle.STALE


def test_apply_patch_created_files_alone_do_not_mark_prior_read_stale():
    messages = _view("read", offset=0, limit=20, truncated=True) + [
        _call("patch", "apply_patch", {"patch": "..."}),
        _result(
            "patch",
            "apply_patch",
            data={"changed_files": [], "deleted_files": [], "created_files": ["a.py"], "moved_files": []},
        ),
    ]

    assert _lifecycle(messages, "read") is ToolResultLifecycle.FRESH


def test_duplicate_derived_output_marks_only_oldest_copy():
    messages = [
        _call("old", "shell", {"command": "pwd"}),
        _result("old", "shell", content="/project"),
        _call("new", "shell", {"command": "pwd"}),
        _result("new", "shell", content="/project"),
    ]

    records = index_tool_result_lifecycles(messages)
    old = records[("tool-old", "result-old")]
    new = records[("tool-new", "result-new")]

    assert old.lifecycle is ToolResultLifecycle.DUPLICATE
    assert old.duplicate_of_part_id == "result-new"
    assert new.lifecycle is ToolResultLifecycle.DERIVED
