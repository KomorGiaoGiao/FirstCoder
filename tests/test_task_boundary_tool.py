from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.task_boundary import create_task_boundary_tool


def test_task_boundary_tool_schema_is_minimal() -> None:
    tool = create_task_boundary_tool(SessionRuntimeState(session_id="sess_test"))

    assert tool.name == "task_boundary"
    assert tool.definition.parameters == {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["same", "new", "uncertain"],
            },
            "basis_message_id": {"type": "string"},
        },
        "required": ["decision", "basis_message_id"],
    }


def test_task_boundary_tool_does_not_accept_model_supplied_hash() -> None:
    tool = create_task_boundary_tool(SessionRuntimeState(session_id="sess_test"))

    result = tool.executor(
        decision="new",
        basis_message_id="msg_new",
        task_hash="model_supplied_hash",
    )

    assert result.ok is False
    assert "不接受模型传入 hash" in result.content


def test_task_boundary_tool_returns_program_generated_candidate_hash() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    tool = create_task_boundary_tool(state, required_stable_count=2)

    first = tool.executor(decision="new", basis_message_id="msg_new")
    second = tool.executor(decision="new", basis_message_id="msg_new")

    assert first.ok is True
    assert first.data["candidate_hash"].startswith("task_")
    assert first.data["confirmed_change"] is False
    assert first.data["should_trigger_compaction"] is False
    assert second.data["candidate_hash"] == first.data["candidate_hash"]
    assert second.data["confirmed_change"] is True
    assert second.data["should_trigger_compaction"] is True
    assert state.active_task_hash == second.data["candidate_hash"]


def test_task_boundary_tool_same_confirms_pending_candidate() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    tool = create_task_boundary_tool(state, required_stable_count=2)

    first = tool.executor(decision="new", basis_message_id="msg_new")
    same = tool.executor(decision="same", basis_message_id="msg_same")

    assert same.ok is True
    assert same.data["candidate_hash"] == first.data["candidate_hash"]
    assert same.data["candidate_basis_message_id"] == "msg_new"
    assert same.data["confirmed_change"] is True
    assert same.data["should_trigger_compaction"] is True
    assert state.active_task_hash == first.data["candidate_hash"]
    assert state.task_hash_stable_count == 0


def test_task_boundary_tool_rejects_invalid_decision() -> None:
    tool = create_task_boundary_tool(SessionRuntimeState(session_id="sess_test"))

    result = tool.executor(decision="maybe", basis_message_id="msg_1")

    assert result.ok is False
    assert "decision 必须是 same、new 或 uncertain" in result.content


def test_task_boundary_tool_rejects_unknown_basis_message_id() -> None:
    registry = create_session_tool_registry(session_id="sess_test", known_message_ids={"msg_known"})

    result = registry.execute("task_boundary", {"decision": "new", "basis_message_id": "msg_missing"})

    assert result.ok is False
    assert "basis_message_id 不属于当前 session" in result.content


def test_task_boundary_tool_supports_single_observation_policy_without_schema_change() -> None:
    registry = create_session_tool_registry(
        session_id="sess_test",
        single_observation_basis_message_ids={"msg_explicit"},
    )

    result = registry.execute("task_boundary", {"decision": "new", "basis_message_id": "msg_explicit"})

    assert result.ok is True
    assert result.data["confirmed_change"] is True
    assert result.data["should_trigger_compaction"] is False
    assert result.data["stable_count"] == 0


def test_session_registry_can_lower_required_stable_count() -> None:
    registry = create_session_tool_registry(
        session_id="sess_test",
        task_boundary_required_stable_count=1,
    )

    result = registry.execute("task_boundary", {"decision": "new", "basis_message_id": "msg_new"})

    assert result.ok is True
    assert result.data["confirmed_change"] is True
    assert result.data["should_trigger_compaction"] is False
    assert result.data["required_stable_count"] == 1
