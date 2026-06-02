"""Agent 会话运行时。

这一层连接 context store、runtime state、system prompt cache 和 session scoped tools。
它不负责调用模型，也不执行压缩；这些动作由更外层的 agent loop 或 context manager 编排。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.agent.tool_flow import assistant_response_to_parts, tool_result_to_part
from firstcoder.context.identity import new_message_id
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.system_prompt import PromptPrefixCache, SystemPromptBuilder, SystemPromptInputs
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall, ToolDefinition
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.tools.types import Tool, ToolResult


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
    provider_capabilities: dict[str, object] = field(
        default_factory=lambda: {"tool_calling": True, "parallel_tool_calls": False},
    )
    permission_policy: dict[str, object] = field(
        default_factory=lambda: {"read": "allow", "write": "confirm", "shell": "confirm"},
    )
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
        registry = create_session_tool_registry(session_id=session_id, runtime_state=runtime_state, tools=tools)
        session = cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=registry,
            writer=SessionEventWriter(store=store, session_id=session_id),
            agents_md=agents_md,
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
        agents_path = Path(project_root) / "AGENTS.md"
        agents_md = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        return cls.create(store=store, session_id=session_id, agents_md=agents_md, tools=tools)

    def append_session_created(self) -> None:
        self.writer.append_session_created()

    def build_system_prefix(self, *, provider_name: str, tools: list[ToolDefinition]) -> list:
        inputs = SystemPromptInputs(
            base_rules=self.base_rules,
            agents_md=self.agents_md,
            tools=tools,
            provider_name=provider_name,
            provider_capabilities=self.provider_capabilities,
            permission_policy=self.permission_policy,
            mode=self.mode,
        )
        entry = self.prompt_cache.get_or_build(inputs, self.prompt_builder)
        self.runtime_state.system_prompt_fingerprint = entry.fingerprint
        return entry.messages

    def append_user_message(self, content: str) -> str:
        return self.writer.append_user_message(content)

    def append_assistant_response(self, response: ChatResponse) -> str:
        message_id = new_message_id()
        return self.writer.append_assistant_parts(
            assistant_response_to_parts(message_id=message_id, response=response),
            message_id=message_id,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )

    def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        return self.tool_registry.execute(tool_call.name, tool_call.arguments)

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:
        message_id = new_message_id()
        return self.writer.append_tool_result_part(
            tool_result_to_part(message_id=message_id, tool_call=tool_call, result=result),
            message_id=message_id,
        )

    def rebuild_view(self):
        return self.store.rebuild_session_view(self.session_id)
