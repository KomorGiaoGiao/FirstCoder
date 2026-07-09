from pathlib import Path

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
