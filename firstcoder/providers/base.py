"""provider 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from firstcoder.providers.types import ChatRequest, ChatResponse


class ChatProvider(ABC):
    """所有模型 provider 都要实现的统一接口。

    agent 主循环只依赖这个接口，不直接依赖 OpenAI、Anthropic 或其他厂商 SDK。
    这样后续切换模型时，只需要替换 provider 实现或配置。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称，例如 `openai`、`deepseek`、`anthropic`。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """当前 provider 默认使用的模型名称。"""

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """同步生成一次回复。"""

    async def acomplete(self, request: ChatRequest) -> ChatResponse:
        """异步生成一次回复。

        多数 Python SDK 的普通接口是同步的，所以这里先用线程包装。
        Textual 后续可以直接 await 这个方法，避免阻塞界面刷新。
        """

        import asyncio

        return await asyncio.to_thread(self.complete, request)

