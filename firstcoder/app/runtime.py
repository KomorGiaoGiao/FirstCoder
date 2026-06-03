"""TUI 运行期 session 状态和聊天入口。

Textual widget 只负责显示和输入；这里把“当前 session 可被 resume 替换”和“普通输入
调用 AgentLoop”封成很薄的一层，避免 UI 直接持有 agent 编排细节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatResponse
from firstcoder.tools.types import Tool


@dataclass(slots=True)
class CurrentSessionState:
    """可替换的当前 session 代理。

    `ContextCommandHandler` 只需要 `session_id`、`runtime_state`、`current_turn` 和
    `rebuild_view()`；把这些属性代理出来后，`/resume` 只要替换内部 session，context
    命令自然会看见新会话。
    """

    session: AgentSession

    def set_session(self, session: AgentSession) -> None:
        self.session = session

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def runtime_state(self) -> SessionRuntimeState:
        return self.session.runtime_state

    @property
    def current_turn(self) -> int:
        return self.session.current_turn

    def rebuild_view(self) -> SessionView:
        return self.session.rebuild_view()


@dataclass(slots=True)
class AgentChatRunner:
    """普通聊天入口，把当前 session 交给 AgentLoop 执行一轮。"""

    current_session: CurrentSessionState
    provider: ChatProvider
    tools: list[Tool] | None = None
    context_builder: ContextBuilder | None = None
    context_manager: Any | None = None
    max_tool_rounds: int = 4
    loops: list[AgentLoop] = field(default_factory=list)

    def run_user_turn(self, content: str) -> ChatResponse:
        loop = AgentLoop(
            session=self.current_session.session,
            provider=self.provider,
            tools=self.tools,
            context_builder=self.context_builder,
            context_manager=self.context_manager,
            max_tool_rounds=self.max_tool_rounds,
        )
        self.loops.append(loop)
        return loop.run_user_turn(content)
