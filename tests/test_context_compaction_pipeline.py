from pathlib import Path

from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.compaction import CompactionPipeline, CompactionRequest
from firstcoder.context.models import AgentMessage, MessagePart, SessionView


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


def test_l1_skips_current_task_content(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old"),
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
    assert old_part.metadata["compaction_state"] == "micro_compacted"
    assert old_part.metadata["compacted_by"] == "l1_old_task"
    assert current_part.content == "当前任务内容" * 80
    assert result.event.stopped_at in {"l1", "l2", "l3", "not_reached"}


def test_compaction_event_records_full_schema(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_old", content="旧任务内容" * 80, task_hash="task_old")],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    assert result.event.event_version == "v1"
    assert result.event.strategy_version == "v1"
    assert result.event.reason in {"l1", "l2", "l3", "not_reached"}
    assert result.event.target_tokens == 1
    assert result.event.source_part_ids == ["part_msg_old"]
    assert result.event.output_part_ids == ["part_msg_old"]
    assert result.event.checkpoint_id is None
    assert result.event.llm_used is False
    assert result.event.success is True
    assert result.event.error is None
    assert result.event.created_at.endswith("Z")


def test_l2_archives_large_tool_result_and_skips_already_archived_part(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool_large",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
            _message(
                "msg_tool_archived",
                role="tool",
                kind="tool_result",
                content="[Tool result archived]\narchive_id=ar_existing",
                task_hash="task_current",
                metadata={
                    "tool_name": "shell",
                    "tool_call_id": "call_2",
                    "compaction_state": "archived",
                    "archive_id": "ar_existing",
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    large_part = result.view.messages[0].parts[0]
    archived_part = result.view.messages[1].parts[0]
    assert large_part.metadata["compaction_state"] == "archived"
    assert large_part.metadata["archive_id"]
    assert "archive_id=" in large_part.content
    assert archived_part.metadata["archive_id"] == "ar_existing"
    assert archived_part.content == "[Tool result archived]\narchive_id=ar_existing"


def test_l3_only_handles_current_task_cold_content(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_cold",
                content="当前任务冷信息" * 120,
                task_hash="task_current",
                created_turn=1,
            ),
            _message(
                "msg_hot",
                content="当前任务热信息" * 120,
                task_hash="task_current",
                created_turn=9,
            ),
            _message(
                "msg_other",
                content="其他任务内容" * 120,
                task_hash="task_other",
                created_turn=1,
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[0].parts[0].metadata["compaction_state"] == "route_compacted"
    assert result.view.messages[0].parts[0].metadata["content_type"] == "plain_text"
    assert result.view.messages[0].parts[0].metadata["route_confidence"] > 0
    assert result.view.messages[1].parts[0].content == "当前任务热信息" * 120
    assert result.view.messages[2].parts[0].content == "其他任务内容" * 120


def test_l3_uses_html_route_compressor(tmp_path: Path) -> None:
    content = "<html><body>" + "".join(f"<p>paragraph {line}</p>" for line in range(1, 160)) + "</body></html>"
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_html_like_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["compaction_state"] == "route_compacted"
    assert part.metadata["content_type"] == "html"
    assert part.metadata["detected_content_type"] == "html"
    assert part.metadata["compacted_by"] == "l3_html"
    assert part.metadata["html_omitted_text_blocks"] > 0


def test_l3_uses_search_results_route_compressor(tmp_path: Path) -> None:
    content = "\n".join(f"firstcoder/app.py:{line}: def function_{line}(): pass" for line in range(1, 20))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_search_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
                metadata={"tool_name": "grep"},
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["content_type"] == "search_results"
    assert part.metadata["compacted_by"] == "l3_search_results"
    assert part.metadata["search_original_matches"] == 19
    assert part.metadata["search_kept_matches"] < 19


def test_l3_uses_git_diff_route_compressor(tmp_path: Path) -> None:
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
                "msg_diff_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
                metadata={"tool_name": "git_diff"},
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["content_type"] == "git_diff"
    assert part.metadata["compacted_by"] == "l3_git_diff"
    assert part.metadata["diff_additions"] == 1
    assert part.metadata["diff_deletions"] == 1


def test_l3_skips_hot_content_by_default(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "diff --git a/firstcoder/app.py b/firstcoder/app.py",
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 80)],
            "-old line",
            "+new line",
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

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[0].parts[0].content == content
    assert result.event.noop is True


def test_l3_can_force_route_compact_hot_current_text(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "diff --git a/firstcoder/app.py b/firstcoder/app.py",
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,4 +1,4 @@",
            *[f" context {line}" for line in range(1, 80)],
            "-old line",
            "+new line",
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

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
            force_route_current_text=True,
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["compaction_state"] == "route_compacted"
    assert part.metadata["content_type"] == "git_diff"
    assert part.metadata["compacted_by"] == "l3_git_diff"
    assert result.event.changed_parts == 1


def test_l3_uses_build_output_route_compressor(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "pytest tests/test_context.py",
            *[f"normal test output line {line}" for line in range(1, 90)],
            "tests/test_context.py::test_resume FAILED",
            "Traceback (most recent call last):",
            '  File "tests/test_context.py", line 33, in test_resume',
            "    assert resume()",
            "AssertionError",
            *[f"more noise {line}" for line in range(90, 160)],
            "FAILED tests/test_context.py::test_resume - AssertionError",
            "1 failed, 12 passed in 1.23s",
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_build_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
                metadata={"tool_name": "pytest"},
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["content_type"] == "build_output"
    assert part.metadata["compacted_by"] == "l3_build_output"
    assert part.metadata["build_omitted_lines"] > 0
    assert "tests/test_context.py::test_resume FAILED" in part.content
    assert "1 failed, 12 passed" in part.content


def test_l3_uses_json_route_compressor(tmp_path: Path) -> None:
    content = (
        "["
        + ",".join(
            '{"id":%d,"status":"%s","message":"%s"}'
            % (
                line,
                "failed" if line == 44 else "ok",
                "ERROR important" if line == 44 else f"normal {line}",
            )
            for line in range(1, 90)
        )
        + "]"
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_json_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["content_type"] == "json_array"
    assert part.metadata["compacted_by"] == "l3_json_array"
    assert part.metadata["json_omitted_items"] > 0
    assert "ERROR important" in part.content


def test_l3_uses_source_code_route_compressor(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "from pathlib import Path",
            "",
            "class ContextBuilder:",
            "    def build(self) -> None:",
            "        first = 1",
            *[f"        intermediate_{line} = {line}" for line in range(1, 90)],
            "        # FIXME important edge case",
            "        raise ValueError('bad boundary')",
            "",
            "def make_builder() -> ContextBuilder:",
            "    return ContextBuilder()",
        ]
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_code_cold",
                content=content,
                task_hash="task_current",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    part = result.view.messages[0].parts[0]
    assert part.metadata["content_type"] == "source_code"
    assert part.metadata["compacted_by"] == "l3_source_code"
    assert part.metadata["code_omitted_lines"] > 0
    assert "class ContextBuilder:" in part.content
    assert "FIXME important edge case" in part.content


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

    assert result.event.stopped_at == "l1"
    assert result.event.levels_attempted == ["l1"]
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
            target_tokens=1,
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
    assert result.view.messages[1].parts[0].metadata["compaction_state"] == "route_compacted"
