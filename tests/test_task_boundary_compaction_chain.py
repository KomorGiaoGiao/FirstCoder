from __future__ import annotations

from pathlib import Path

from benchmark.task_boundary_compaction.chain import clone_recorded_session
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.task_boundary import TaskBoundaryService
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.types import make_text_result


def test_clone_recorded_session_preserves_a_context_but_not_a_project_files(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture-data"
    capture_store = JsonlSessionStore(capture_root)
    session = AgentSession.create(store=capture_store, session_id="benchmark", agents_md="")
    first_message_id = session.append_user_message("任务 A：分析代码")
    observation = TaskBoundaryService().initialize_active_task(
        session.runtime_state,
        basis_message_id=first_message_id,
    )
    assert observation is not None
    session.writer.append_task_boundary_observation(observation)
    tool_call = ToolCall(id="read-a", name="read_file", arguments={"path": "A.java"})
    session.append_assistant_response(
        ChatResponse(provider="fake", model="fake", content="读取 A", tool_calls=[tool_call])
    )
    session.append_tool_result(
        tool_call=tool_call,
        result=make_text_result("read_file", "class A {}"),
    )
    session.append_user_message("继续任务 A：实现修复")
    session.append_assistant_response(ChatResponse(provider="fake", model="fake", content="完成 A"))
    (tmp_path / "a-project").mkdir()
    (tmp_path / "a-project" / "A.java").write_text("class A {}", encoding="utf-8")

    clone_root = tmp_path / "b-data"
    cloned_store = clone_recorded_session(
        source_data_root=capture_root,
        destination_data_root=clone_root,
        session_id="benchmark",
    )
    resumed = AgentSession.resume(store=cloned_store, session_id="benchmark", agents_md="")
    view = resumed.rebuild_view()

    assert resumed.runtime_state.active_task_hash == session.runtime_state.active_task_hash
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert any(part.kind == "tool_result" for message in view.messages for part in message.parts)
    assert not (clone_root / "A.java").exists()
    assert (tmp_path / "a-project" / "A.java").exists()
