from pathlib import Path

from firstcoder.context.archive import ToolResultArchive
from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.compaction import CompactionPipeline, CompactionRequest
from firstcoder.context.identity import session_view_fingerprint
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.tool_sequence import validate_tool_call_sequence


def _message(
    message_id: str,
    *,
    role: str = "user",
    kind: str = "text",
    content: str = "content",
    task_hash: str = "task_current",
    created_turn: int = 10,
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    part_metadata = {
        "task_hash": task_hash,
        "created_turn": created_turn,
    }
    if metadata:
        part_metadata.update(metadata)
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind=kind,
                content=content,
                metadata=part_metadata,
            )
        ],
    )


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> AgentMessage:
    message_id = f"msg_call_{call_id}"
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id=f"part_call_{call_id}",
                message_id=message_id,
                kind="tool_call",
                content="",
                metadata={
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "arguments": arguments,
                },
            )
        ],
    )


def _tool_result(
    call_id: str,
    name: str,
    *,
    content: str,
    data: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    message_id = f"msg_result_{call_id}"
    result_metadata: dict[str, object] = {
        "tool_call_id": call_id,
        "tool_name": name,
        "ok": True,
        "data": data or {},
    }
    if metadata:
        result_metadata.update(metadata)
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id=f"part_result_{call_id}",
                message_id=message_id,
                kind="tool_result",
                content=content,
                metadata=result_metadata,
            )
        ],
    )


def test_session_view_fingerprint_tracks_persisted_message_content() -> None:
    original = SessionView(session_id="sess_test", messages=[_message("msg_1", content="original")])
    same = SessionView(session_id="sess_test", messages=[_message("msg_1", content="original")])
    changed = SessionView(session_id="sess_test", messages=[_message("msg_1", content="changed")])

    assert session_view_fingerprint(original) == session_view_fingerprint(same)
    assert session_view_fingerprint(original) != session_view_fingerprint(changed)
    assert len(session_view_fingerprint(original)) == 24


def test_l1_skips_current_task_content(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old", created_turn=1),
            _message("msg_current", content="当前任务内容" * 80, task_hash="task_current"),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    old_part = result.view.messages[0].parts[0]
    current_part = result.view.messages[1].parts[0]
    # The second message is the latest user message and must be retained.  A
    # normal AUTO L1 pass only trims sufficiently old cross-task dialogue.
    assert old_part.metadata["compaction_state"] == "trimmed"
    assert old_part.metadata["compacted_by"] == "l1_old_task_dialogue"
    assert old_part.content == ""
    assert current_part.content == "当前任务内容" * 80
    assert result.event.stopped_at in {"l1", "l2", "l3", "not_reached"}


def test_compaction_event_records_full_schema(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old", created_turn=1),
            _message("msg_latest", content="latest", task_hash="task_current", created_turn=10),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    assert result.event.event_version == "v2"
    assert result.event.strategy_version == "v2"
    assert result.event.reason in {"l1", "l2", "l3", "not_reached"}
    assert result.event.target_tokens == 1
    assert result.event.source_part_ids == ["part_msg_old"]
    assert result.event.output_part_ids == ["part_msg_old"]
    assert result.event.checkpoint_id is None
    assert result.event.llm_used is False
    assert result.event.success is True
    assert result.event.error is None
    assert result.event.created_at.endswith("Z")


def test_l2_routes_derived_search_and_stores_raw_backing(tmp_path: Path) -> None:
    raw_content = "\n".join(f"firstcoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("search_l2", "grep", {"pattern": "function"}),
            _tool_result("search_l2", "grep", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    part = result.view.messages[1].parts[0]
    assert part.metadata["compaction_state"] == "l2_route_compacted"
    assert part.metadata["compacted_by"] == "l2_search_results"
    assert part.metadata["lifecycle"] == "derived"
    assert part.metadata["tool_call_id"] == "search_l2"
    assert part.metadata["replacement_tokens"] < part.metadata["original_tokens"]
    record, backed = ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])
    assert backed == raw_content
    assert record.content_sha256 == part.metadata["original_content_sha256"]


def test_l2_never_routes_fresh_source_and_does_not_create_backing(tmp_path: Path) -> None:
    raw_content = "source line\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_l2_fresh", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_l2_fresh",
                "view",
                content=raw_content,
                data={
                    "path": "firstcoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") is None
    assert not (tmp_path / "archives").exists()


def test_l2_skips_when_router_has_no_strictly_smaller_candidate(tmp_path: Path) -> None:
    raw_content = "small derived result"
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("small_l2", "shell", {"command": "echo small"}),
            _tool_result("small_l2", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert not (tmp_path / "archives").exists()


def test_l2_routes_build_and_diff_derived_results_with_raw_backing(tmp_path: Path) -> None:
    build_raw = "\n".join(
        [
            "pytest tests/test_context.py",
            *[f"normal test output line {line}" for line in range(1, 130)],
            "tests/test_context.py::test_resume FAILED",
            "Traceback (most recent call last):",
            '  File "tests/test_context.py", line 33, in test_resume',
            "AssertionError",
            "1 failed, 12 passed in 1.23s",
        ]
    )
    diff_raw = "\n".join(
        [
            "diff --git a/firstcoder/app.py b/firstcoder/app.py",
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 100)],
            "-old line",
            "+new line",
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("build_l2", "pytest", {"command": "pytest"}),
            _tool_result("build_l2", "pytest", content=build_raw),
            _tool_call("diff_l2", "git_diff", {"path": "firstcoder/app.py"}),
            _tool_result("diff_l2", "git_diff", content=diff_raw),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2",),
        )
    )

    build_part = result.view.messages[1].parts[0]
    diff_part = result.view.messages[3].parts[0]
    assert build_part.metadata["compacted_by"] == "l2_build_output"
    assert build_part.metadata["build_omitted_lines"] > 0
    assert diff_part.metadata["compacted_by"] == "l2_git_diff"
    assert diff_part.metadata["diff_context_lines_omitted"] > 0
    assert ToolResultArchive(tmp_path).read("sess_test", build_part.metadata["archive_id"])[1] == build_raw
    assert ToolResultArchive(tmp_path).read("sess_test", diff_part.metadata["archive_id"])[1] == diff_raw
    validate_tool_call_sequence(result.view.messages)


def test_l2_then_l3_uses_existing_raw_backing_and_is_idempotent(tmp_path: Path) -> None:
    raw_content = "\n".join(f"firstcoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("l2_l3", "grep", {"pattern": "function"}),
            _tool_result("l2_l3", "grep", content=raw_content),
        ],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = CompactionRequest(
        view=view,
        active_task_hash="task_current",
        target_tokens=1,
        current_turn=10,
        enabled_levels=("l2", "l3"),
        l2_result_target_tokens=20,
    )

    first = pipeline.compact(request)
    archived = first.view.messages[1].parts[0]
    second = pipeline.compact(
        CompactionRequest(
            view=first.view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l2", "l3"),
            l2_result_target_tokens=20,
        )
    )

    assert archived.metadata["compaction_state"] == "archived"
    assert archived.metadata["compacted_by"] == "l3_archive"
    assert ToolResultArchive(tmp_path).read("sess_test", archived.metadata["archive_id"])[1] == raw_content
    assert second.event.changed_parts == 0
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1
    validate_tool_call_sequence(first.view.messages)
    validate_tool_call_sequence(second.view.messages)


def test_per_result_pressure_runs_l2_then_l3_below_total_budget(tmp_path: Path) -> None:
    raw_content = "\n".join(f"firstcoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 160))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_l2_l3", "grep", {"pattern": "function"}),
            _tool_result("pressure_l2_l3", "grep", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2", "l3"),
            l2_result_target_tokens=1,
        )
    )

    part = result.view.messages[1].parts[0]
    assert result.event.levels_attempted == ["l2", "l3"]
    assert result.event.changed_parts == 2
    assert result.event.replacements[0]["replacement_part"]["metadata"]["compacted_by"] == "l2_search_results"
    assert part.metadata["compaction_state"] == "archived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_per_result_pressure_does_not_bypass_fresh_source_noop(tmp_path: Path) -> None:
    raw_content = "source line\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_fresh", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "pressure_fresh",
                "view",
                content=raw_content,
                data={
                    "path": "firstcoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2", "l3"),
            l2_result_target_tokens=1,
        )
    )

    assert result.event.levels_attempted == []
    assert result.event.stopped_at == "already_within_budget"
    assert result.view.messages[1].parts[0].content == raw_content
    assert not (tmp_path / "archives").exists()


def test_per_result_pressure_archives_raw_derived_below_total_budget(tmp_path: Path) -> None:
    raw_content = "plain shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pressure_raw", "shell", {"command": "long-command"}),
            _tool_result("pressure_raw", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l2", "l3"),
            l2_result_target_tokens=1,
        )
    )

    part = result.view.messages[1].parts[0]
    assert result.event.levels_attempted == ["l2", "l3"]
    assert part.metadata["compaction_state"] == "archived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_l3_archives_raw_derived_result_when_over_budget(tmp_path: Path) -> None:
    raw_content = "plain shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("raw_l3", "shell", {"command": "long-command"}),
            _tool_result("raw_l3", "shell", content=raw_content),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
            l2_result_target_tokens=20,
        )
    )

    part = result.view.messages[1].parts[0]
    assert part.metadata["compaction_state"] == "archived"
    assert part.metadata["lifecycle"] == "derived"
    assert ToolResultArchive(tmp_path).read("sess_test", part.metadata["archive_id"])[1] == raw_content


def test_l3_skips_pinned_derived_result(tmp_path: Path) -> None:
    raw_content = "pinned result\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("pinned_l3", "shell", {"command": "long-command"}),
            _tool_result(
                "pinned_l3",
                "shell",
                content=raw_content,
                metadata={"compaction_state": "pinned"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[1].parts[0].content == raw_content
    assert result.event.noop is True
    assert not (tmp_path / "archives").exists()


def test_l3_never_routes_text_even_when_force_flag_is_set(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "diff --git a/firstcoder/app.py b/firstcoder/app.py",
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 40)],
            "-old line",
            "+new line",
            *[f" more context {line}" for line in range(40, 80)],
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_diff_hot",
                content=content,
                task_hash="task_current",
                created_turn=10,
                metadata={"tool_name": "git_diff"},
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l3",),
            force_route_current_text=True,
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.content == content
    assert part.metadata.get("compaction_state") is None
    assert result.event.noop is True


def test_pipeline_stops_after_budget_target_is_met(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 200, task_hash="task_old"),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1000,
            current_turn=10,
        )
    )

    assert result.event.stopped_at in {"l1", "l2", "l3", "not_reached"}
    assert result.event.levels_attempted[0] == "l1"
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") != "archived"


def test_pipeline_does_nothing_when_already_within_budget(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old"),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=10_000,
            current_turn=10,
        )
    )

    assert result.event.noop is True
    assert result.event.levels_attempted == []
    assert result.event.stopped_at == "already_within_budget"
    assert result.view.messages[0].parts[0].content == "旧任务内容" * 80
    assert result.view.messages[1].parts[0].content == "large tool output\n" * 200
    assert not (tmp_path / "archives").exists()


def test_already_within_budget_noop_is_deduped(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_current", content="short", task_hash="task_current")],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = CompactionRequest(
        view=view,
        active_task_hash="task_current",
        target_tokens=10_000,
        current_turn=10,
    )

    first = pipeline.compact(request)
    second = pipeline.compact(request)

    assert first.event.noop is True
    assert first.event.deduped is False
    assert second.event.noop is True
    assert second.event.deduped is True


def test_l1_does_not_compact_old_task_tool_call_or_tool_result_chain(tmp_path: Path) -> None:
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
                        metadata={
                            "task_hash": "task_old",
                            "tool_call_id": "call_1",
                            "tool_name": "shell",
                            "arguments": {"command": "git status"},
                        },
                    )
                ],
            ),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="旧任务工具结果" * 120,
                task_hash="task_old",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    assert result.view.messages[0].parts[0].kind == "tool_call"
    assert result.view.messages[0].parts[0].metadata["tool_call_id"] == "call_1"
    assert result.view.messages[1].parts[0].kind == "tool_result"
    assert result.view.messages[1].parts[0].content == "旧任务工具结果" * 120
    assert result.event.noop is True


def test_l1_task_switch_immediately_trims_old_dialogue_but_never_latest_user_or_tool_call_text(tmp_path: Path) -> None:
    old_user = _message("msg_old_user", content="old user" * 80, task_hash="task_old", created_turn=10)
    old_assistant_with_tool = AgentMessage(
        id="msg_old_assistant_tool",
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id="part_old_assistant_text",
                message_id="msg_old_assistant_tool",
                kind="text",
                content="old explanation" * 80,
                metadata={"task_hash": "task_old", "created_turn": 10},
            ),
            MessagePart(
                id="part_old_call",
                message_id="msg_old_assistant_tool",
                kind="tool_call",
                content="",
                metadata={"tool_call_id": "call_old", "tool_name": "shell", "arguments": {}},
            ),
        ],
    )
    old_result = _tool_result("call_old", "shell", content="old tool output")
    latest_user = _message("msg_latest", content="latest requirement" * 80, task_hash="task_old", created_turn=10)
    view = SessionView(
        session_id="sess_test",
        messages=[old_user, old_assistant_with_tool, old_result, latest_user],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_new",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
            force_old_task_compaction=True,
        )
    )

    assert result.view.messages[0].parts[0].metadata["compaction_state"] == "trimmed"
    assert result.view.messages[0].parts[0].content == ""
    assert result.view.messages[1].parts[0].content == "old explanation" * 80
    assert result.view.messages[-1].parts[0].content == "latest requirement" * 80
    validate_tool_call_sequence(result.view.messages)


def test_l1_auto_waits_for_old_task_cold_turn_distance(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="old" * 80, task_hash="task_old", created_turn=9),
            _message("msg_latest", content="latest", task_hash="task_current", created_turn=10),
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    assert result.view.messages[0].parts[0].content == "old" * 80
    assert result.event.noop is True


def test_noop_compaction_is_recorded_and_deduped(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_current", content="short", task_hash="task_current")],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = CompactionRequest(
        view=view,
        active_task_hash="task_current",
        target_tokens=1,
        current_turn=10,
    )

    first = pipeline.compact(request)
    second = pipeline.compact(request)

    assert first.event.noop is True
    assert first.event.input_fingerprint == second.event.input_fingerprint
    assert second.event.deduped is True


def test_pipeline_does_not_replace_part_when_compaction_would_increase_tokens(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_short_cold",
                content="短",
                task_hash="task_current",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[0].parts[0].content == "短"
    assert result.event.noop is True


def test_l1_l3_skip_checkpoint_covered_history(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_checkpointed", content="旧任务已 checkpoint" * 160, task_hash="task_old", created_turn=1),
            _message("msg_tail", content="当前 tail 内容" * 160, task_hash="task_current", created_turn=1),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="旧历史摘要",
                tail_start_message_id="msg_tail",
                covered_until_message_id="msg_checkpointed",
                source_fingerprint="fp_1",
                sequence=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    assert result.view.messages[0].parts[0].content == "旧任务已 checkpoint" * 160
    assert "part_msg_checkpointed" not in result.event.source_part_ids
    assert result.view.messages[1].parts[0].content == "当前 tail 内容" * 160
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") is None


def test_l3_keeps_fresh_large_view_and_structured_tool_call_byte_identical(tmp_path: Path) -> None:
    call = _tool_call("view_fresh", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 500})
    raw_call = call.parts[0].to_dict()
    raw_content = "source line\n" * 1_000
    result = _tool_result(
        "view_fresh",
        "view",
        content=raw_content,
        data={
            "path": "firstcoder/context.py",
            "start_line": 1,
            "end_line": 500,
            "total_lines": 2_000,
            "truncated": True,
        },
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=SessionView(session_id="sess_test", messages=[call, result]),
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert compacted.view.messages[0].parts[0].to_dict() == raw_call
    assert compacted.view.messages[1].parts[0].content == raw_content
    assert compacted.view.messages[1].parts[0].metadata["data"] == result.parts[0].metadata["data"]
    assert compacted.event.lifecycle_counts["fresh"] == 1
    assert compacted.event.noop is True
    assert not (tmp_path / "archives").exists()


def test_l3_archives_stale_view_and_keeps_raw_backing(tmp_path: Path) -> None:
    raw_content = "before edit source\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_stale", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_stale",
                "view",
                content=raw_content,
                data={
                    "path": "firstcoder/context.py",
                    "start_line": 1,
                    "end_line": 500,
                    "total_lines": 2_000,
                    "truncated": True,
                },
            ),
            _tool_call("edit_stale", "edit", {"path": "firstcoder/context.py"}),
            _tool_result("edit_stale", "edit", content="updated", data={"path": "firstcoder/context.py"}),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    archived = compacted.view.messages[1].parts[0]
    assert archived.kind == "tool_result"
    assert archived.metadata["lifecycle"] == "stale"
    assert archived.metadata["lifecycle_reason"] == "source_mutated"
    assert "lifecycle=stale" in archived.content
    assert raw_content not in archived.content
    record, backed = ToolResultArchive(tmp_path).read("sess_test", archived.metadata["archive_id"])
    assert backed == raw_content
    assert record.archive_id == archived.metadata["archive_id"]
    assert compacted.event.lifecycle_counts["stale"] == 1


def test_l3_archives_superseded_view_after_later_covering_view(tmp_path: Path) -> None:
    first_content = "first read\n" * 1_000
    second_content = "later source of truth\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_first", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 100}),
            _tool_result(
                "view_first",
                "view",
                content=first_content,
                data={"path": "firstcoder/context.py", "start_line": 1, "end_line": 100, "total_lines": 500, "truncated": True},
            ),
            _tool_call("view_second", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 500}),
            _tool_result(
                "view_second",
                "view",
                content=second_content,
                data={"path": "firstcoder/context.py", "start_line": 1, "end_line": 500, "total_lines": 500, "truncated": False},
            ),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    old_read = compacted.view.messages[1].parts[0]
    new_read = compacted.view.messages[3].parts[0]
    assert old_read.metadata["lifecycle"] == "superseded"
    assert "lifecycle=superseded" in old_read.content
    assert new_read.content == second_content
    assert compacted.event.lifecycle_counts["superseded"] == 1
    assert compacted.event.lifecycle_counts["fresh"] == 1


def test_l3_archives_duplicate_derived_results_under_the_same_content_addressed_id(tmp_path: Path) -> None:
    raw_content = "derived shell output\n" * 1_000
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("duplicate_one", "shell", {"command": "pwd"}),
            _tool_result("duplicate_one", "shell", content=raw_content),
            _tool_call("duplicate_two", "shell", {"command": "pwd"}),
            _tool_result("duplicate_two", "shell", content=raw_content),
            _tool_call("duplicate_three", "shell", {"command": "pwd"}),
            _tool_result("duplicate_three", "shell", content=raw_content),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l3",),
            l2_result_target_tokens=10_000,
        )
    )

    first = compacted.view.messages[1].parts[0]
    second = compacted.view.messages[3].parts[0]
    latest = compacted.view.messages[5].parts[0]
    assert first.metadata["lifecycle"] == second.metadata["lifecycle"] == "duplicate"
    assert first.metadata["archive_id"] == second.metadata["archive_id"]
    assert latest.content == raw_content
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1
    assert compacted.event.lifecycle_counts["duplicate"] == 2
    assert compacted.event.lifecycle_counts["derived"] == 1


def test_l3_skips_current_turn_protected_archive_retrieval_duplicate(tmp_path: Path) -> None:
    raw_content = "retrieved archive content\n" * 1_000
    protected = {"data": {"archive_retrieval": True, "compaction_protected_until_turn": 10}}
    view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("retrieval_one", "retrieve_archive", {"archive_id": "ar_example"}),
            _tool_result("retrieval_one", "retrieve_archive", content=raw_content, metadata=protected),
            _tool_call("retrieval_two", "retrieve_archive", {"archive_id": "ar_example"}),
            _tool_result("retrieval_two", "retrieve_archive", content=raw_content, metadata=protected),
        ],
    )

    compacted = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert compacted.view.messages[1].parts[0].content == raw_content
    assert compacted.view.messages[1].parts[0].metadata.get("compaction_state") is None
    assert compacted.event.lifecycle_counts["duplicate"] == 1
    assert not (tmp_path / "archives").exists()


def test_required_l3_runs_below_budget_and_repeat_is_idempotent(tmp_path: Path) -> None:
    raw_content = "stale source\n" * 1_000
    source_view = SessionView(
        session_id="sess_test",
        messages=[
            _tool_call("view_required", "view", {"path": "firstcoder/context.py", "offset": 0, "limit": 20}),
            _tool_result(
                "view_required",
                "view",
                content=raw_content,
                data={"path": "firstcoder/context.py", "start_line": 1, "end_line": 20, "total_lines": 200, "truncated": True},
            ),
            _tool_call("write_required", "write", {"path": "firstcoder/context.py"}),
            _tool_result("write_required", "write", content="updated", data={"path": "firstcoder/context.py"}),
        ],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = CompactionRequest(
        view=source_view,
        active_task_hash="task_current",
        target_tokens=100_000,
        current_turn=10,
        enabled_levels=("l3",),
        required_levels=("l3",),
    )

    first = pipeline.compact(request)
    second = pipeline.compact(
        CompactionRequest(
            view=first.view,
            active_task_hash="task_current",
            target_tokens=100_000,
            current_turn=10,
            enabled_levels=("l3",),
            required_levels=("l3",),
        )
    )

    assert first.event.levels_attempted == ["l3"]
    assert first.event.stopped_at == "l3"
    assert first.event.changed_parts == 1
    assert first.view.messages[1].parts[0].metadata["compaction_state"] == "archived"
    assert second.event.levels_attempted == ["l3"]
    assert second.event.changed_parts == 0
    assert second.event.noop is True
    assert len(list((tmp_path / "archives" / "sess_test").glob("*.txt"))) == 1


def test_task_switch_compaction_saves_context_without_breaking_tool_transaction(tmp_path: Path) -> None:
    """A task switch must release old dialogue/output while retaining legal calls."""

    old_dialogue = "old-task discussion that is no longer actionable\n" * 240
    old_search_output = "\n".join(f"firstcoder/old_module.py:{line}: obsolete_symbol_{line}" for line in range(1, 500))
    current_request = "Implement the new task and keep this request visible."
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_old_dialogue",
                content=old_dialogue,
                task_hash="task_old",
                created_turn=1,
            ),
            _tool_call("old_search", "grep", {"pattern": "obsolete_symbol"}),
            _tool_result("old_search", "grep", content=old_search_output),
            _message(
                "msg_current_request",
                content=current_request,
                task_hash="task_current",
                created_turn=20,
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=20,
            force_old_task_compaction=True,
            required_levels=("l2", "l3"),
            l2_result_target_tokens=12,
        )
    )

    assert result.event.levels_attempted == ["l1", "l2", "l3"]
    assert result.event.level_metrics["l1"]["changed_parts"] == 1
    assert result.event.level_metrics["l2"]["changed_parts"] == 1
    assert result.event.level_metrics["l3"]["changed_parts"] == 1
    assert result.event.after_tokens * 4 < result.event.before_tokens
    assert result.view.messages[0].parts[0].metadata["compaction_state"] == "trimmed"
    assert result.view.messages[2].parts[0].metadata["compaction_state"] == "archived"
    assert result.view.messages[3].parts[0].content == current_request
    validate_tool_call_sequence(result.view.messages)
