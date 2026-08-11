"""Benchmark 完成门禁与停滞检测的聚焦测试。"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field

from firstcoder.agent.background import BackgroundJobManager
from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.agent.stagnation import StagnationGuard
from firstcoder.context.store import JsonlSessionStore
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderCapabilities,
    ToolCall,
    ToolDefinition,
)
from firstcoder.tools.types import Tool, ToolResult, make_text_result
from firstcoder.tools.write import create_write_tool
from firstcoder.tools.ask_user import create_ask_user_tool


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if _is_boundary_request(request):
            return _boundary_response(request, provider=self.name, model=self.model)
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass
class StreamingProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake-stream"

    @property
    def model(self) -> str:
        return "fake-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if _is_boundary_request(request):
            return _boundary_response(request, provider=self.name, model=self.model)
        raise AssertionError("streaming test should not fall back to complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        yield ChatStreamEvent(kind="message_started")
        if response.content:
            yield ChatStreamEvent(kind="text_delta", text=response.content)
        for tool_call in response.tool_calls:
            yield ChatStreamEvent(
                kind="tool_call_started",
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )
            yield ChatStreamEvent(kind="tool_call_completed", tool_call=tool_call)
        yield ChatStreamEvent(kind="message_completed", response=response)


def test_benchmark_completion_gate_requires_validation_after_mutation(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_gate",
        tools=[create_write_tool(tmp_path), _successful_validation_tool()],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "result.txt", "content": "done"},
                )
            ),
            _text_response("应该完成了"),
            _tool_response(
                ToolCall(
                    id="call_verify",
                    name="shell",
                    arguments={"command": "pytest -q"},
                )
            ),
            _text_response("已验证完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        "生成 result.txt"
    )

    assert result.content == "已验证完成"
    assert len(provider.requests) == 4
    assert _has_completion_instruction(provider.requests[2])


def test_benchmark_completion_gate_skips_after_post_mutation_validation(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_verified",
        tools=[create_write_tool(tmp_path), _successful_validation_tool()],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "result.txt", "content": "done"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_verify",
                    name="shell",
                    arguments={"command": "pytest -q"},
                )
            ),
            _text_response("完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        "生成并验证"
    )

    assert result.content == "完成"
    assert len(provider.requests) == 3
    assert not any(_has_completion_instruction(request) for request in provider.requests)


def test_benchmark_completion_gate_requires_final_probe_of_explicit_url_target(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_explicit_target",
        tools=[create_write_tool(tmp_path), _successful_validation_tool()],
        task=(
            "Deploy the result so curl http://server:8080/hello.html returns hello world."
        ),
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "hello.html", "content": "hello world"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_target_probe",
                    name="shell",
                    arguments={"command": "curl http://server:8080/hello.html"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_cleanup",
                    name="shell",
                    arguments={"command": "rm -f scratch.tmp; curl http://server:8080/"},
                )
            ),
            _text_response("完成"),
            _tool_response(
                ToolCall(
                    id="call_final_target_probe",
                    name="shell",
                    arguments={
                        "command": (
                            "test \"$(curl --fail http://server:8080/hello.html)\" "
                            "= \"hello world\""
                        )
                    },
                )
            ),
            _text_response("最终状态已验证"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "最终状态已验证"
    assert _has_completion_instruction(provider.requests[4])
    assert "/hello.html" in _system_text(provider.requests[4])


def test_benchmark_completion_gate_treats_http_404_output_as_failed_validation(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_http_404",
        tools=[create_write_tool(tmp_path), _http_probe_tool()],
        task="Ensure curl http://server:8080/hello.html returns hello world.",
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "hello.html", "content": "hello world"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_bad_probe",
                    name="shell",
                    arguments={
                        "command": (
                            "curl -s -o /dev/null -w 'HTTP_%{http_code}' "
                            "http://server:8080/hello.html"
                        )
                    },
                )
            ),
            _text_response("完成"),
            _text_response("目标仍返回 HTTP 404，不能宣称完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "目标仍返回 HTTP 404，不能宣称完成"
    assert _has_completion_instruction(provider.requests[3])
    assert "most recent validation (shell) failed" in _system_text(provider.requests[3])


def test_benchmark_completion_gate_accepts_explicitly_expected_http_404(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_expected_http_404",
        tools=[create_write_tool(tmp_path), _http_probe_tool()],
        task="Ensure curl http://server:8080/removed.html returns HTTP 404.",
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "removed.html", "content": "removed"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_expected_404",
                    name="shell",
                    arguments={
                        "command": (
                            "test \"$(curl -s -o /dev/null -w '%{http_code}' "
                            "http://server:8080/removed.html)\" = 404"
                        )
                    },
                )
            ),
            _text_response("预期 404 已验证"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "预期 404 已验证"
    assert not any(_has_completion_instruction(request) for request in provider.requests)


def test_benchmark_completion_gate_rejects_asserted_state_that_conflicts_with_expected_output(
    tmp_path,
) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_conflicting_http_state",
        tools=[create_write_tool(tmp_path), _http_probe_tool()],
        task=(
            "Configure the server so if I run\n"
            "    curl http://server:8080/hello.html\n"
            'then I see the output "hello world".'
        ),
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "hello.html", "content": "hello world"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_wrong_final_assertion",
                    name="shell",
                    arguments={
                        "command": (
                            "test \"$(curl -s -o /dev/null -w '%{http_code}' "
                            "http://server:8080/hello.html)\" = 404"
                        )
                    },
                )
            ),
            _text_response("完成"),
            _text_response("最终响应正文尚未恢复，不能宣称完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "最终响应正文尚未恢复，不能宣称完成"
    assert _has_completion_instruction(provider.requests[3])
    instruction = _system_text(provider.requests[3])
    assert "/hello.html -> output 'hello world'" in instruction


def test_benchmark_completion_gate_uses_final_target_assertion_within_one_script(
    tmp_path,
) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_final_target_assertion",
        tools=[create_write_tool(tmp_path), _http_probe_tool()],
        task='Ensure curl http://server:8080/hello.html returns "hello world".',
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "hello.html", "content": "hello world"},
                )
            ),
            _tool_response(
                ToolCall(
                    id="call_mixed_final_state",
                    name="shell",
                    arguments={
                        "command": (
                            "response=$(curl --fail http://server:8080/hello.html)\n"
                            "test \"$response\" = \"hello world\"\n"
                            "rm -f /srv/www/hello.html\n"
                            "status=$(curl -s -o /dev/null -w '%{http_code}' "
                            "http://server:8080/hello.html)\n"
                            "test \"$status\" = 404"
                        )
                    },
                )
            ),
            _text_response("完成"),
            _text_response("最终目标已被清理，不能宣称完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "最终目标已被清理，不能宣称完成"
    assert _has_completion_instruction(provider.requests[3])


def test_benchmark_prompt_preserves_task_required_state_after_validation(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_state_preservation",
        tools=[],
    )
    provider = FakeProvider([_text_response("done")])

    AgentLoop(session=session, provider=provider)._run_user_turn_sync("finish")

    prompt = _system_text(provider.requests[0])
    assert "Preserve task-required commits, deployed files" in prompt
    assert "Never replace a working required state with a pristine state by default." in prompt
    assert "A probe that merely prints HTTP 404/500" in prompt


def test_benchmark_runtime_hides_and_rejects_web_search(tmp_path) -> None:
    calls: list[str] = []

    def execute(query: str) -> ToolResult:
        calls.append(query)
        return make_text_result("web_search", "unexpected")

    tool = Tool(
        definition=ToolDefinition(
            name="web_search",
            description="Search the web",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        executor=execute,
    )
    session = _benchmark_session(
        tmp_path,
        session_id="sess_benchmark_no_web_search",
        tools=[tool],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_search",
                    name="web_search",
                    arguments={"query": "task-specific answer"},
                )
            ),
            _text_response("未使用搜索"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "未使用搜索"
    assert "web_search" not in [tool.name for tool in provider.requests[0].tools]
    assert calls == []
    search_result = next(
        part
        for part in _tool_result_parts(session)
        if part.metadata.get("tool_name") == "web_search"
    )
    assert search_result.metadata["data"]["tool_not_routed"] is True


def test_benchmark_completion_gate_runs_at_most_once_after_failed_validation(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_once",
        tools=[_failed_validation_tool()],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_verify",
                    name="shell",
                    arguments={"command": "pytest -q"},
                )
            ),
            _text_response("先结束"),
            _text_response("验证仍失败，存在真实阻塞"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        "修复并验证"
    )

    assert result.content == "验证仍失败，存在真实阻塞"
    assert len(provider.requests) == 3
    assert sum(_has_completion_instruction(request) for request in provider.requests) == 1


def test_benchmark_completion_gate_rechecks_once_after_post_gate_mutation(tmp_path) -> None:
    def execute(command: str) -> ToolResult:
        return ToolResult(
            name="shell",
            ok=True,
            content="HTTP_404",
            data={
                "command": command,
                "exit_code": 0,
                "stdout": "HTTP_404",
                "stderr": "",
            },
        )

    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_rearmed",
        tools=[create_write_tool(tmp_path), _shell_tool(execute)],
        task='Ensure curl http://server:8080/hello.html returns "hello world".',
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "hello.html", "content": "hello world"},
                )
            ),
            _text_response("先结束"),
            _tool_response(
                ToolCall(
                    id="call_cleanup",
                    name="shell",
                    arguments={
                        "command": (
                            "rm -f /srv/www/hello.html; "
                            "test \"$(curl -s -o /dev/null -w '%{http_code}' "
                            "http://server:8080/hello.html)\" = 404"
                        )
                    },
                )
            ),
            _text_response("清理后结束"),
            _text_response("最终目标被清理，不能宣称完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "最终目标被清理，不能宣称完成"
    gate_requests = [request for request in provider.requests if _has_completion_instruction(request)]
    assert len(gate_requests) == 2
    assert "/hello.html -> output 'hello world'" in _system_text(gate_requests[-1])


def test_benchmark_system_prompt_repeats_explicit_final_acceptance_contract(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_acceptance_contract",
        tools=[],
        task='Ensure curl http://server:8080/hello.html returns "hello world".',
    )
    provider = FakeProvider([_text_response("done")])

    AgentLoop(session=session, provider=provider)._run_user_turn_sync(session.benchmark_task)

    prompt = _system_text(provider.requests[0])
    assert "Task-derived final acceptance contract" in prompt
    assert "/hello.html must preserve response body 'hello world'" in prompt
    assert "Preserve these outcomes after validation and cleanup" in prompt


def test_benchmark_completion_gate_has_streaming_parity(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_stream",
        tools=[create_write_tool(tmp_path), _successful_validation_tool()],
    )
    provider = StreamingProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "result.txt", "content": "done"},
                ),
                provider="fake-stream",
                model="fake-stream-model",
            ),
            _text_response(
                "先结束",
                provider="fake-stream",
                model="fake-stream-model",
            ),
            _tool_response(
                ToolCall(
                    id="call_verify",
                    name="shell",
                    arguments={"command": "pytest -q"},
                ),
                provider="fake-stream",
                model="fake-stream-model",
            ),
            _text_response(
                "流式验证完成",
                provider="fake-stream",
                model="fake-stream-model",
            ),
        ]
    )

    turn = asyncio.run(
        AgentLoop(session=session, provider=provider).run_user_turn(
            "生成 result.txt",
            streaming=True,
        )
    )

    assert turn.content == "流式验证完成"
    assert len(provider.requests) == 4
    assert _has_completion_instruction(provider.requests[2])


def test_permission_resume_observes_mutation_for_completion_gate(tmp_path) -> None:
    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_permission_resume",
        tools=[create_write_tool(tmp_path), _successful_validation_tool()],
        bypass=False,
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_write",
                    name="write",
                    arguments={"path": "result.txt", "content": "done"},
                )
            ),
            _text_response("写入完成"),
            _tool_response(
                ToolCall(
                    id="call_verify",
                    name="shell",
                    arguments={"command": "pytest -q"},
                )
            ),
            _text_response("确认完成"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    paused = loop._run_user_turn_sync("写入 result.txt")
    assert paused.pending_input is not None
    result = loop._resume_with_user_input_sync(paused.pending_input.id, "allow_once")

    assert result.content == "确认完成"
    assert _has_completion_instruction(provider.requests[2])


def test_stagnation_warns_then_blocks_fourth_identical_failure(tmp_path) -> None:
    counter: dict[str, int] = {}
    session = _benchmark_session(
        tmp_path,
        session_id="sess_stagnation_block",
        tools=[_counting_failed_shell(counter)],
    )
    provider = FakeProvider(
        [
            *[
                _tool_response(
                    ToolCall(
                        id=f"call_probe_{index}",
                        name="shell",
                        arguments={"command": "probe"},
                    )
                )
                for index in range(1, 5)
            ],
            _text_response("改用其他策略"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        "诊断失败"
    )

    assert result.content == "改用其他策略"
    assert counter["calls"] == 3
    tool_parts = _tool_result_parts(session)
    assert "same failure twice" in tool_parts[1].content
    assert "three times" in tool_parts[2].content
    assert tool_parts[3].metadata["data"]["stagnation_blocked"] is True


def test_stagnation_resets_on_new_user_turn(tmp_path) -> None:
    counter: dict[str, int] = {}
    session = _benchmark_session(
        tmp_path,
        session_id="sess_stagnation_turn_reset",
        tools=[_counting_failed_shell(counter)],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(id="call_1", name="shell", arguments={"command": "probe"})
            ),
            _text_response("第一轮结束"),
            _tool_response(
                ToolCall(id="call_2", name="shell", arguments={"command": "probe"})
            ),
            _text_response("第二轮结束"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    loop._run_user_turn_sync("第一轮")
    loop._run_user_turn_sync("第二轮")

    assert counter["calls"] == 2
    assert all(
        "agent_guidance" not in part.metadata["data"]
        for part in _tool_result_parts(session)
    )


def test_running_background_job_triggers_completion_gate(tmp_path) -> None:
    release = threading.Event()
    started = threading.Event()
    manager = BackgroundJobManager(max_workers=1)

    def execute(command: str) -> ToolResult:
        started.set()
        release.wait(5)
        return make_text_result("shell", f"done:{command}")

    try:
        session = _benchmark_session(
            tmp_path,
            session_id="sess_background_completion_gate",
            tools=[_shell_tool(execute)],
        )
        provider = FakeProvider(
            [
                _tool_response(
                    ToolCall(
                        id="call_background",
                        name="shell",
                        arguments={
                            "command": "serve",
                            "run_in_background": True,
                        },
                    )
                ),
                _text_response("服务已启动"),
                _text_response("后台任务仍在运行，暂不宣称完成"),
            ]
        )

        result = AgentLoop(
            session=session,
            provider=provider,
            background_manager=manager,
        )._run_user_turn_sync("启动服务")

        assert started.wait(2)
        assert result.content == "后台任务仍在运行，暂不宣称完成"
        assert _has_completion_instruction(provider.requests[2])
        assert "background jobs are still running" in _system_text(provider.requests[2])
    finally:
        release.set()
        manager.wait(timeout=5)
        manager.shutdown()


def test_background_status_polling_uses_independent_non_blocking_rule() -> None:
    guard = StagnationGuard()
    call = ToolCall(
        id="call_status",
        name="background_status",
        arguments={"job_id": "bg_0001"},
    )
    result = make_text_result(
        "background_status",
        "bg_0001: shell -> running",
        job={"job_id": "bg_0001", "tool_name": "shell", "status": "running"},
    )

    assert guard.observe(call, result) is None
    assert guard.observe(call, result) is None
    warning = guard.observe(call, result)

    assert warning is not None
    assert "three status checks" in warning
    assert guard.validate(call) is None


def test_git_diff_does_not_reset_stagnation_failure_chain() -> None:
    guard = StagnationGuard()
    failed_call = ToolCall(
        id="call_probe",
        name="shell",
        arguments={"command": "probe"},
    )
    failed_result = ToolResult(
        name="shell",
        ok=False,
        content="probe failed",
        error="probe failed",
        data={"exit_code": 1},
    )
    diff_call = ToolCall(id="call_diff", name="git_diff", arguments={"path": "."})
    diff_result = make_text_result(
        "git_diff",
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@",
    )

    assert guard.observe(failed_call, failed_result) is None
    assert "same failure twice" in (guard.observe(failed_call, failed_result) or "")
    assert guard.observe(diff_call, diff_result) is None
    assert "three times" in (guard.observe(failed_call, failed_result) or "")
    assert guard.validate(failed_call) is not None


def _benchmark_session(
    tmp_path,
    *,
    session_id: str,
    tools: list[Tool],
    bypass: bool = True,
    task: str = "完成当前 benchmark 任务",
) -> AgentSession:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id=session_id,
        project_root=tmp_path,
        tools=tools,
    )
    if bypass:
        session.set_permission_mode(PermissionMode.BYPASS)
    session.set_benchmark_task(task)
    return session


def test_benchmark_completion_gate_requires_validation_after_process_start(tmp_path) -> None:
    def execute(command: str) -> ToolResult:
        return make_text_result(
            "process_start",
            "长期进程 proc_0001 已启动。",
            process={"process_id": "proc_0001", "status": "running"},
        )

    session = _benchmark_session(
        tmp_path,
        session_id="sess_completion_process_start",
        tools=[_named_tool("process_start", execute)],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_start",
                    name="process_start",
                    arguments={"command": "python -m http.server 8080"},
                )
            ),
            _text_response("服务已启动，完成"),
            _text_response("已确认服务状态后再结束"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        "启动服务"
    )

    assert result.content == "已确认服务状态后再结束"
    assert _has_completion_instruction(provider.requests[2])
    assert "has not been validated afterwards" in _system_text(provider.requests[2])


def test_benchmark_runtime_hides_and_rejects_ask_user(tmp_path) -> None:
    calls: list[str] = []

    def execute(question: str, options: list[str] | None = None) -> ToolResult:
        calls.append(question)
        return make_text_result("ask_user", "unexpected", requires_user_input=True)

    tool = _named_tool("ask_user", execute, params=("question",))
    session = _benchmark_session(
        tmp_path,
        session_id="sess_benchmark_no_ask_user",
        tools=[tool],
    )
    provider = FakeProvider(
        [
            _tool_response(
                ToolCall(
                    id="call_ask",
                    name="ask_user",
                    arguments={"question": "should I continue?"},
                )
            ),
            _text_response("未向用户提问，自主完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync(
        session.benchmark_task
    )

    assert result.content == "未向用户提问，自主完成"
    assert "ask_user" not in [tool.name for tool in provider.requests[0].tools]
    assert calls == []
    ask_result = next(
        part
        for part in _tool_result_parts(session)
        if part.metadata.get("tool_name") == "ask_user"
    )
    assert ask_result.metadata["data"]["tool_not_routed"] is True


def test_interactive_runtime_still_exposes_registered_ask_user(tmp_path) -> None:
    session = AgentSession.from_project(
        store=JsonlSessionStore(tmp_path / ".firstcoder"),
        session_id="sess_interactive_ask_user",
        project_root=tmp_path,
        tools=[create_ask_user_tool()],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
    provider = FakeProvider([_text_response("done")])

    AgentLoop(session=session, provider=provider)._run_user_turn_sync("finish")

    assert "ask_user" in [tool.name for tool in provider.requests[0].tools]


def _successful_validation_tool() -> Tool:
    def execute(command: str) -> ToolResult:
        return ToolResult(
            name="shell",
            ok=True,
            content="3 passed",
            data={
                "command": command,
                "exit_code": 0,
                "stdout": "3 passed",
                "stderr": "",
            },
        )

    return _shell_tool(execute)


def _failed_validation_tool() -> Tool:
    def execute(command: str) -> ToolResult:
        return ToolResult(
            name="shell",
            ok=False,
            content="1 failed",
            data={
                "command": command,
                "exit_code": 1,
                "stdout": "",
                "stderr": "1 failed",
            },
            error="命令退出码为 1",
        )

    return _shell_tool(execute)


def _http_probe_tool() -> Tool:
    def execute(command: str) -> ToolResult:
        return ToolResult(
            name="shell",
            ok=True,
            content="HTTP_404",
            data={
                "command": command,
                "exit_code": 0,
                "stdout": "HTTP_404",
                "stderr": "",
            },
        )

    return _shell_tool(execute)


def _counting_failed_shell(counter: dict[str, int]) -> Tool:
    def execute(command: str) -> ToolResult:
        counter["calls"] = counter.get("calls", 0) + 1
        return ToolResult(
            name="shell",
            ok=False,
            content="probe failed",
            data={
                "command": command,
                "exit_code": 1,
                "stdout": "",
                "stderr": "probe failed",
            },
            error="命令退出码为 1",
        )

    return _shell_tool(execute)


def _shell_tool(executor) -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="shell",
            description="测试 shell",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        ),
        executor=executor,
    )


def _named_tool(
    name: str,
    executor,
    *,
    params: tuple[str, ...] = ("command",),
) -> Tool:
    return Tool(
        definition=ToolDefinition(
            name=name,
            description=f"测试 {name}",
            parameters={
                "type": "object",
                "properties": {param: {"type": "string"} for param in params},
                "required": [params[0]],
            },
        ),
        executor=executor,
    )


def _tool_response(
    tool_call: ToolCall,
    *,
    provider: str = "fake",
    model: str = "fake-model",
) -> ChatResponse:
    return ChatResponse(
        provider=provider,
        model=model,
        content="",
        tool_calls=[tool_call],
        finish_reason="tool_calls",
    )


def _text_response(
    content: str,
    *,
    provider: str = "fake",
    model: str = "fake-model",
) -> ChatResponse:
    return ChatResponse(provider=provider, model=model, content=content)


def _is_boundary_request(request: ChatRequest) -> bool:
    return request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512


def _boundary_response(
    request: ChatRequest,
    *,
    provider: str,
    model: str,
) -> ChatResponse:
    basis_message_id = "msg_unknown"
    for message in reversed(request.messages):
        match = re.search(r"basis_message_id=([A-Za-z0-9_]+)", message.content)
        if match:
            basis_message_id = match.group(1)
            break
    return ChatResponse(
        provider=provider,
        model=model,
        content=(
            '{"decision":"uncertain","basis_message_id":"'
            + basis_message_id
            + '"}'
        ),
    )


def _has_completion_instruction(request: ChatRequest) -> bool:
    return "Benchmark completion check" in _system_text(request)


def _system_text(request: ChatRequest) -> str:
    return "\n".join(
        message.content for message in request.messages if message.role == "system"
    )


def _tool_result_parts(session: AgentSession):
    return [
        part
        for message in session.rebuild_view().messages
        if message.role == "tool"
        for part in message.parts
        if part.kind == "tool_result"
    ]
