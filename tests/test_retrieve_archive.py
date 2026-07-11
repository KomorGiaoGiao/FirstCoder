from __future__ import annotations

import json

import pytest

from firstcoder.agent.session import AgentSession
from firstcoder.agent.tool_flow import tool_result_to_part
from firstcoder.context.archive import ToolResultArchive
from firstcoder.context.models import MessagePart
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.types import ToolCall
from firstcoder.session.fork import ForkSessionService
from firstcoder.tools import create_builtin_registry
from firstcoder.tools.retrieve_archive import create_retrieve_archive_tool
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.think import create_think_tool


def _seed(tmp_path, content: str, *, session_id: str = "sess_test") -> str:
    part = MessagePart(
        id="part_result",
        message_id="msg_tool",
        kind="tool_result",
        content=content,
        metadata={"tool_name": "shell"},
    )
    return ToolResultArchive(tmp_path).store_original(session_id, part).archive_id


def _tool(tmp_path, turn=lambda: 7, *, session_id: str = "sess_test"):
    return create_retrieve_archive_tool(session_id=session_id, archive_root=tmp_path, current_turn=turn)


def test_schema_and_full_retrieval_are_bounded_and_protected(tmp_path) -> None:
    archive_id = _seed(tmp_path, "abcdefghij")
    tool = _tool(tmp_path, turn=lambda: 9)

    result = tool.executor(archive_id=archive_id, full=True, max_chars=4)

    assert tool.definition.parameters["required"] == ["archive_id"]
    assert tool.definition.parameters["properties"]["max_chars"]["default"] == 6000
    assert tool.definition.parameters["properties"]["max_chars"]["minimum"] == 1
    assert tool.definition.parameters["properties"]["max_chars"]["maximum"] == 12000
    assert result.ok is True
    assert result.data == {
        "archive_retrieval": True,
        "compaction_protected_until_turn": 9,
        "archive_id": archive_id,
        "query": None,
        "full": True,
        "match_count": 0,
        "returned_chars": 4,
        "truncated": True,
        "original_tokens": result.data["original_tokens"],
        "content_sha256": result.data["content_sha256"],
    }
    assert len(result.content) <= 4
    assert result.content == "abcd"


def test_query_is_case_insensitive_numbered_and_merges_context_windows(tmp_path) -> None:
    archive_id = _seed(tmp_path, "one\ntwo needle\nthree\nfour\nneedle five\nsix\nseven")

    result = _tool(tmp_path).executor(archive_id=archive_id, query="NEEDLE", max_chars=500)

    assert result.ok is True
    assert result.data["match_count"] == 2
    assert result.data["truncated"] is False
    assert "1: one" in result.content
    assert "7: seven" in result.content
    assert "[... omitted ...]" not in result.content


def test_truncated_query_uses_omission_marker_when_budget_allows(tmp_path) -> None:
    archive_id = _seed(tmp_path, "\n".join(f"line {number} needle" for number in range(30)))

    result = _tool(tmp_path).executor(archive_id=archive_id, query="needle", max_chars=80)

    assert result.ok is True
    assert result.data["truncated"] is True
    assert len(result.content) <= 80
    assert result.content.endswith("[... omitted ...]")


def test_no_match_and_empty_query_diagnostic_are_bounded(tmp_path) -> None:
    archive_id = _seed(tmp_path, "BEGIN-" + "x" * 500 + "-END")
    tool = _tool(tmp_path)

    no_match = tool.executor(archive_id=archive_id, query="missing", max_chars=8)
    diagnostic = tool.executor(archive_id=archive_id, query="  ", max_chars=500)

    assert no_match.ok is True
    assert no_match.data["match_count"] == 0
    assert len(no_match.content) <= 8
    assert no_match.data["truncated"] is True
    assert diagnostic.ok is True
    assert diagnostic.data["query"] is None
    assert diagnostic.data["full"] is False
    assert diagnostic.data["match_count"] == 0
    assert len(diagnostic.content) <= 500
    assert f"archive_id={archive_id}" in diagnostic.content
    assert "original_tokens=" in diagnostic.content
    assert "BEGIN-" in diagnostic.content
    assert "-END" in diagnostic.content
    assert "query" in diagnostic.content
    assert "full=true" in diagnostic.content

    arbitrary_cap = tool.executor(archive_id=archive_id, max_chars=200)
    assert arbitrary_cap.ok is True
    assert len(arbitrary_cap.content) <= 200
    if "Head:" in arbitrary_cap.content:
        assert "Tail:" in arbitrary_cap.content


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars": 0},
        {"max_chars": 12001},
        {"max_chars": True},
        {"query": 1},
        {"full": 1},
        {"unexpected": "argument"},
    ],
)
def test_invalid_arguments_fail_safely(tmp_path, kwargs) -> None:
    archive_id = _seed(tmp_path, "content")

    result = _tool(tmp_path).executor(archive_id=archive_id, **kwargs)

    assert result.ok is False
    assert str(tmp_path) not in result.content


def test_missing_tampered_or_cross_session_archive_fails_without_path(tmp_path) -> None:
    archive_id = _seed(tmp_path, "content")
    other_session = _tool(tmp_path, session_id="other_session")

    cross_session = other_session.executor(archive_id=archive_id)
    missing = _tool(tmp_path).executor(archive_id="ar_missing")
    metadata_path = tmp_path / "archives" / "sess_test" / f"{archive_id}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["content_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    tampered = _tool(tmp_path).executor(archive_id=archive_id)

    for result in (cross_session, missing, tampered):
        assert result.ok is False
        assert "archives" not in result.content
        assert str(tmp_path) not in result.content


def test_session_registry_injects_retrieve_and_rejects_override(tmp_path) -> None:
    registry = create_session_tool_registry(session_id="sess_test", archive_root=tmp_path)

    assert "retrieve_archive" in registry.names()
    with pytest.raises(ValueError, match="reserved"):
        create_session_tool_registry(
            session_id="sess_test",
            archive_root=tmp_path,
            tools=[create_think_tool()] + [_tool(tmp_path, session_id="other")],
        )


def test_builtin_registry_does_not_include_session_bound_retrieval(tmp_path) -> None:
    assert "retrieve_archive" not in create_builtin_registry(tmp_path).names()


def test_session_create_and_resume_have_dynamic_retrieval_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    created = AgentSession.create(store=store, session_id="sess_test")
    assert "retrieve_archive" in created.tool_registry.names()
    created.writer.append_user_message("first turn")
    assert created.tool_registry.execute("retrieve_archive", {"archive_id": "ar_missing"}).ok is False

    resumed = AgentSession.resume(store=store, session_id="sess_test")
    archive_id = _seed(tmp_path, "payload")
    result = resumed.tool_registry.execute("retrieve_archive", {"archive_id": archive_id, "full": True})
    assert result.ok is True
    assert result.data["compaction_protected_until_turn"] == resumed.current_turn == 1


def test_forked_session_retrieves_copied_v2_archive_and_keeps_archives_session_local(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    source = AgentSession.create(store=store, session_id="sess_source")
    raw_content = "original archive content\nwith an important detail"
    archive_id = _seed(tmp_path, raw_content, session_id=source.session_id)

    forked = ForkSessionService(store=store, project_root=tmp_path).fork(source.session_id)
    forked_session = forked.session
    forked_id = forked_session.session_id

    assert forked_id != source.session_id
    assert "retrieve_archive" in forked_session.tool_registry.names()
    retrieved = forked_session.tool_registry.execute(
        "retrieve_archive",
        {"archive_id": archive_id, "full": True},
    )
    assert retrieved.ok is True
    assert retrieved.content == raw_content
    assert retrieved.data["archive_id"] == archive_id

    source_archive_dir = tmp_path / "archives" / source.session_id
    fork_archive_dir = tmp_path / "archives" / forked_id
    assert source_archive_dir != fork_archive_dir
    assert (source_archive_dir / f"{archive_id}.txt").read_text(encoding="utf-8") == raw_content
    assert (fork_archive_dir / f"{archive_id}.txt").read_text(encoding="utf-8") == raw_content
    assert (source_archive_dir / f"{archive_id}.json").read_bytes() == (
        fork_archive_dir / f"{archive_id}.json"
    ).read_bytes()

    fork_only_id = _seed(tmp_path, "fork-only backing", session_id=forked_id)
    assert not (source_archive_dir / f"{fork_only_id}.txt").exists()
    assert source.tool_registry.execute(
        "retrieve_archive",
        {"archive_id": fork_only_id, "full": True},
    ).ok is False
    assert forked_session.tool_registry.execute(
        "retrieve_archive",
        {"archive_id": fork_only_id, "full": True},
    ).content == "fork-only backing"


def test_tool_result_to_part_copies_retrieval_data(tmp_path) -> None:
    archive_id = _seed(tmp_path, "payload")
    result = _tool(tmp_path, turn=lambda: 11).executor(archive_id=archive_id, full=True)
    call = ToolCall(id="call_retrieve", name="retrieve_archive", arguments={"archive_id": archive_id})

    part = tool_result_to_part(message_id="msg_result", tool_call=call, result=result)

    assert part.metadata["data"] == result.data
    assert part.metadata["data"]["archive_retrieval"] is True
    assert part.metadata["data"]["compaction_protected_until_turn"] == 11
