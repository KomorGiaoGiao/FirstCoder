"""Agent 会话运行时。

这一层连接 context store、runtime state、system prompt cache 和 session scoped tools。
它不负责调用模型，也不执行压缩；这些动作由更外层的 agent loop 或 context manager 编排。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from firstcoder.agent.prompt_inputs import (
    DEFAULT_PERMISSION_POLICY,
    build_system_prompt_inputs,
    read_agents_md,
)
from firstcoder.agent.tool_flow import assistant_response_to_parts, tool_result_to_part
from firstcoder.context.identity import new_message_id
from firstcoder.context.runtime_replay import replay_runtime_state
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.system_prompt import PromptPrefixCache, SystemPromptBuilder
from firstcoder.context.task_boundary import observation_from_tool_result_data
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall, ToolDefinition
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.types import Tool, ToolResult
from firstcoder.context.models import AgentMessage, MessagePart


DEFAULT_BASE_RULES = "你是 FirstCoder，一个本地 AI coding agent。请遵守项目规则并优先保持上下文可恢复。"


@dataclass(slots=True)
class AgentSession:
    """单个会话的运行时容器。

    `SessionRuntimeState` 和 `PromptPrefixCache` 都是运行期对象，不写成自然语言消息。
    真正可 resume 的会话事实通过 `JsonlSessionStore` 追加事件保存。
    """

    session_id: str
    store: JsonlSessionStore
    runtime_state: SessionRuntimeState
    tool_registry: ToolRegistry
    writer: SessionEventWriter
    agents_md: str = ""
    base_rules: str = DEFAULT_BASE_RULES
    prompt_cache: PromptPrefixCache = field(default_factory=PromptPrefixCache)
    prompt_builder: SystemPromptBuilder = field(default_factory=SystemPromptBuilder)
    provider_capability_overrides: dict[str, object] = field(default_factory=dict)
    permission_policy: dict[str, object] = field(default_factory=lambda: dict(DEFAULT_PERMISSION_POLICY))
    known_message_ids: set[str] = field(default_factory=set)
    turn_counter: int = 0
    mode: str = "default"

    @classmethod
    def create(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        agents_md: str = "",
        tools: list[Tool] | None = None,
    ) -> "AgentSession":
        runtime_state = SessionRuntimeState(session_id=session_id)
        known_message_ids: set[str] = set()
        registry = create_session_tool_registry(
            session_id=session_id,
            runtime_state=runtime_state,
            tools=tools,
            known_message_ids=known_message_ids,
        )
        session = cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=registry,
            writer=SessionEventWriter(store=store, session_id=session_id),
            agents_md=agents_md,
            known_message_ids=known_message_ids,
            turn_counter=0,
        )
        session.append_session_created()
        return session

    @classmethod
    def from_project(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        project_root: str | Path,
        tools: list[Tool] | None = None,
    ) -> "AgentSession":
        agents_md = read_agents_md(project_root)
        return cls.create(store=store, session_id=session_id, agents_md=agents_md, tools=tools)

    @classmethod
    def resume(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        agents_md: str = "",
        tools: list[Tool] | None = None,
    ) -> "AgentSession":
        """从 JSONL 会话日志恢复运行期 session。

        `rebuild_session_view()` 恢复可投影的消息和 checkpoint；`replay_runtime_state()`
        恢复 task hash、compact 熔断和最近压缩事实。这里还要把历史 message id 注入
        `known_message_ids`，否则恢复后的 task_boundary 工具会拒绝模型引用旧消息。
        """

        runtime_state = replay_runtime_state(store, session_id)
        view = store.rebuild_session_view(session_id)
        known_message_ids = {message.id for message in view.messages}
        turn_counter = _infer_turn_counter(view.messages)
        registry = create_session_tool_registry(
            session_id=session_id,
            runtime_state=runtime_state,
            tools=tools,
            known_message_ids=known_message_ids,
        )
        return cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=registry,
            writer=SessionEventWriter(store=store, session_id=session_id, current_turn=turn_counter),
            agents_md=agents_md,
            known_message_ids=known_message_ids,
            turn_counter=turn_counter,
        )

    def append_session_created(self) -> None:
        self.writer.append_session_created()

    def build_system_prefix(
        self,
        *,
        provider_name: str,
        provider_model: str = "",
        tools: list[ToolDefinition],
    ) -> list:
        inputs = build_system_prompt_inputs(
            base_rules=self.base_rules,
            agents_md=self.agents_md,
            tools=tools,
            provider_name=provider_name,
            provider_model=provider_model,
            provider_capability_overrides=self.provider_capability_overrides,
            permission_policy=self.permission_policy,
            mode=self.mode,
        )
        entry = self.prompt_cache.get_or_build(inputs, self.prompt_builder)
        self.runtime_state.system_prompt_fingerprint = entry.fingerprint
        return entry.messages

    def append_user_message(self, content: str) -> str:
        message_id = self.writer.append_user_message(
            content,
            part_metadata=self._current_context_metadata(),
        )
        self.turn_counter = self.writer.current_turn
        self.known_message_ids.add(message_id)
        return message_id

    def append_assistant_response(self, response: ChatResponse) -> str:
        message_id = new_message_id()
        parts = assistant_response_to_parts(message_id=message_id, response=response)
        self._attach_current_context_metadata(parts)
        assistant_message_id = self.writer.append_assistant_parts(
            parts,
            message_id=message_id,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": asdict(response.usage) if response.usage is not None else None,
                "diagnostics": asdict(response.diagnostics),
            },
        )
        self.known_message_ids.add(assistant_message_id)
        return assistant_message_id

    def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        return self.tool_registry.execute(tool_call.name, tool_call.arguments)

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:
        message_id = new_message_id()
        part = tool_result_to_part(message_id=message_id, tool_call=tool_call, result=result)
        self._attach_current_context_metadata([part])
        tool_message_id = self.writer.append_tool_result_part(
            part,
            message_id=message_id,
        )
        self.known_message_ids.add(tool_message_id)
        self._append_task_boundary_observation_if_present(tool_call=tool_call, result=result)
        return tool_message_id

    @property
    def current_turn(self) -> int:
        return self.writer.current_turn

    def rebuild_view(self):
        return self.store.rebuild_session_view(self.session_id)

    def _append_task_boundary_observation_if_present(self, *, tool_call: ToolCall, result: ToolResult) -> None:
        if tool_call.name != "task_boundary" or not result.ok:
            return
        observation = observation_from_tool_result_data(result.data)
        if observation is not None:
            self.writer.append_task_boundary_observation(observation)

    def _current_context_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if self.runtime_state.active_task_hash:
            metadata["task_hash"] = self.runtime_state.active_task_hash
        return metadata

    def _attach_current_context_metadata(self, parts: list[MessagePart]) -> None:
        metadata = self._current_context_metadata()
        for part in parts:
            part.metadata.update(metadata)


def _infer_turn_counter(messages: list[AgentMessage]) -> int:
    """从已恢复的消息里推断下一轮 turn 编号。"""

    return sum(1 for message in messages if message.role == "user")
