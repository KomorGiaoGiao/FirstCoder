"""Agent 主循环最小闭环。"""

from __future__ import annotations

from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.tools.types import Tool


class AgentLoop:
    """把用户输入、上下文投影、provider 调用和工具执行串成一轮会话。

    当前只实现上下文闭环需要的最小同步流程：用户消息落库、构造 system prefix、投影
    provider messages、处理一轮或多轮 tool calls。自动压缩和 provider 错误恢复留给后续
    `ContextWindowManager` 阶段接入。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        tools: list[Tool] | None = None,
        context_builder: ContextBuilder | None = None,
        max_tool_rounds: int = 4,
    ) -> None:
        self.session = session
        self.provider = provider
        self.context_builder = context_builder or ContextBuilder()
        self.max_tool_rounds = max_tool_rounds
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)

    def run_user_turn(self, content: str) -> ChatResponse:
        self.session.append_user_message(content)

        response = self._complete_once()
        tool_rounds = 0
        while response.tool_calls:
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break

            self.session.append_assistant_response(response)
            self._execute_tool_calls(response.tool_calls)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break
            response = self._complete_once()

        self.session.append_assistant_response(response)
        return response

    def _complete_once(self) -> ChatResponse:
        definitions = self.session.tool_registry.definitions()
        system_prefix = self.session.build_system_prefix(provider_name=self.provider.name, tools=definitions)
        messages = self.context_builder.build_provider_messages(
            self.session.rebuild_view(),
            system_prefix=system_prefix,
        )
        return self.provider.complete(ChatRequest(messages=messages, tools=definitions))

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        for tool_call in tool_calls:
            result = self.session.execute_tool_call(tool_call)
            self.session.append_tool_result(tool_call=tool_call, result=result)

    def _tool_round_limit_response(self, response: ChatResponse) -> ChatResponse:
        """工具轮次上限命中后，只保存纯文本说明，避免写入未执行的 tool_call。"""

        return ChatResponse(
            provider=response.provider,
            model=response.model,
            content=f"工具调用轮次达到上限（max_tool_rounds={self.max_tool_rounds}），已停止继续执行工具。",
            tool_calls=[],
            finish_reason="tool_round_limit",
            raw=response.raw,
        )
