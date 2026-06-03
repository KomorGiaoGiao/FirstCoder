from firstcoder.session import (
    RedactionOptions,
    SessionCorruptError,
    SessionError,
    SessionInvalidIdError,
    SessionNotFoundError,
    SessionRecord,
    ShareOptions,
    Transcript,
    TranscriptEntry,
)
from firstcoder.session.catalog import SessionCatalog


def test_session_package_exports_core_models_and_errors() -> None:
    record = SessionRecord(session_id="sess_test", title="测试会话")
    entry = TranscriptEntry(role="user", title="User", content="你好", message_id="msg_1")
    transcript = Transcript(session=record, entries=[entry])

    assert record.status == "ok"
    assert record.message_count == 0
    assert transcript.entries[0].content == "你好"
    assert RedactionOptions().redact_paths is True
    assert ShareOptions().include_tool_results is False
    assert issubclass(SessionNotFoundError, SessionError)
    assert issubclass(SessionInvalidIdError, SessionError)
    assert issubclass(SessionCorruptError, SessionError)


def test_session_catalog_boundary_exists_without_textual_dependency(tmp_path) -> None:
    catalog = SessionCatalog(tmp_path)

    assert isinstance(catalog, SessionCatalog)
