from pathlib import Path

from firstcoder.context.compaction import CompactionEvent
from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.eval.context_metrics import collect_context_metrics
from firstcoder.providers.types import ChatResponse


def test_collect_context_metrics_reads_session_transcript(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_metrics")
    writer.append_user_message("hello")
    writer.append_assistant_response(ChatResponse(provider="fake", model="fake", content="world"))

    metrics = collect_context_metrics(tmp_path / "sessions" / "sess_metrics.jsonl")

    assert metrics["transcript_exists"] is True
    assert metrics["events"] == 2
    assert metrics["messages"] == 2
    assert metrics["estimated_tokens"] > 0
    assert metrics["compaction_events"] == 0


def test_collect_context_metrics_reports_missing_transcript(tmp_path: Path) -> None:
    metrics = collect_context_metrics(tmp_path / "missing.jsonl")

    assert metrics == {
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "transcript_exists": False,
    }


def test_collect_context_metrics_reports_additive_compaction_archive_and_tool_metrics(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_metrics_detail"
    writer = SessionEventWriter(store=store, session_id=session_id)
    writer.append_tool_result(
        tool_call=_call("call_view_1", "view"),
        result=_result("first read", data={"path": "src/app.py"}),
    )
    writer.append_tool_result(
        tool_call=_call("call_multi", "read_multi"),
        result=_result("multi read", data={"files": [{"path": "src/app.py"}, {"path": "src/lib.py"}]}),
    )
    writer.append_tool_result(
        tool_call=_call("call_view_2", "view"),
        result=_result("reread", data={"path": "./src/app.py"}),
    )
    writer.append_tool_result(
        tool_call=_call("call_retrieve_ok", "retrieve_archive"),
        result=_result("retrieved", data={"content_type": "archive_match"}),
    )
    writer.append_tool_result(
        tool_call=_call("call_retrieve_failed", "retrieve_archive"),
        result=_result("missing", ok=False, data={}),
    )
    writer.append_compaction_completed(
        trigger="auto",
        target_tokens=100,
        event=CompactionEvent(
            input_fingerprint="fp",
            before_tokens=300,
            after_tokens=180,
            levels_attempted=["l1", "l2"],
            stopped_at="l2",
            changed_parts=2,
            level_metrics={
                "l1": {"before_tokens": 300, "after_tokens": 250, "saved_tokens": 50, "changed_parts": 1},
                "l2": {"before_tokens": 250, "after_tokens": 180, "saved_tokens": 70, "changed_parts": 1},
            },
        ),
    )
    store.append_event(
        SessionEvent(
            id="evt_l4",
            session_id=session_id,
            type="llm_compaction_completed",
            payload={"status": "success", "event": {"status": "success"}},
        )
    )
    archive_dir = tmp_path / "archives" / session_id
    archive_dir.mkdir(parents=True)
    archive_payload = b"archive original bytes"
    (archive_dir / "ar_test.txt").write_bytes(archive_payload)
    (archive_dir / "ar_test.json").write_text("{}", encoding="utf-8")

    metrics = collect_context_metrics(tmp_path / "sessions" / f"{session_id}.jsonl")

    assert metrics["compaction_before_tokens_total"] == 300
    assert metrics["compaction_after_tokens_total"] == 180
    assert metrics["compaction_token_savings_total"] == 120
    assert metrics["compaction_level_token_savings"] == {"l1": 50, "l2": 70}
    assert metrics["archive_count"] == 1
    assert metrics["archive_bytes"] == len(archive_payload)
    assert metrics["retrieve_archive_success_count"] == 1
    assert metrics["l4_completion_count"] == 1
    assert metrics["tool_result_tool_names"] == {
        "view": 2,
        "read_multi": 1,
        "retrieve_archive": 2,
    }
    assert metrics["tool_result_content_types"] == {}
    assert metrics["source_reread_count"] == 2


def test_collect_context_metrics_reads_content_types_from_effective_replacements(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_metrics_replacement_type"
    writer = SessionEventWriter(store=store, session_id=session_id)
    message_id = writer.append_tool_result(
        tool_call=_call("call_json", "shell"),
        result=_result('{"very":"large"}', data={}),
    )
    raw_part = store.rebuild_session_view(session_id).messages[0].parts[0]
    replacement = raw_part.to_dict()
    replacement["content"] = '{"summary":"compact"}'
    replacement["metadata"].update(
        {
            "content_type": "json_object",
            "compaction_state": "l2_route_compacted",
        }
    )
    writer.append_compaction_completed(
        trigger="auto",
        target_tokens=10,
        event=CompactionEvent(
            input_fingerprint="replacement-fingerprint",
            before_tokens=20,
            after_tokens=10,
            levels_attempted=["l2"],
            stopped_at="l2",
            changed_parts=1,
            replacements=[
                {
                    "message_id": message_id,
                    "source_part_id": raw_part.id,
                    "replacement_part": replacement,
                }
            ],
        ),
    )

    metrics = collect_context_metrics(tmp_path / "sessions" / f"{session_id}.jsonl")

    assert metrics["tool_result_tool_names"] == {"shell": 1}
    assert metrics["tool_result_content_types"] == {"json_object": 1}


def test_collect_context_metrics_is_compatible_with_legacy_or_missing_fields(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_metrics_legacy"
    store.append_event(
        SessionEvent(
            id="evt_legacy_compact",
            session_id=session_id,
            type="compaction_completed",
            payload={"before_tokens": 40, "after_tokens": 25, "event": {"changed_parts": 1}},
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_bad_tool",
            session_id=session_id,
            type="tool_result",
            payload={
                "message_id": "msg_legacy_tool",
                "parts": [
                    {
                        "id": "part_legacy_tool",
                        "kind": "tool_result",
                        "content": "failed legacy read",
                        "metadata": {"tool_name": "view", "ok": False},
                    }
                ],
            },
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_legacy_l4",
            session_id=session_id,
            type="llm_compaction_completed",
            payload={"status": "failed"},
        )
    )

    metrics = collect_context_metrics(tmp_path / "sessions" / f"{session_id}.jsonl")

    assert metrics["compaction_before_tokens_total"] == 40
    assert metrics["compaction_after_tokens_total"] == 25
    assert metrics["compaction_level_token_savings"] == {}
    assert metrics["archive_count"] == 0
    assert metrics["archive_bytes"] == 0
    assert metrics["retrieve_archive_success_count"] == 0
    assert metrics["l4_completion_count"] == 0
    assert metrics["tool_result_tool_names"] == {"view": 1}
    assert metrics["tool_result_content_types"] == {}
    assert metrics["source_reread_count"] == 0


def _call(call_id: str, name: str):
    from firstcoder.providers.types import ToolCall

    return ToolCall(id=call_id, name=name, arguments={})


def _result(content: str, *, ok: bool = True, data: dict[str, object]):
    from firstcoder.tools.types import ToolResult

    return ToolResult(name="test", ok=ok, content=content, data=data)
