"""工具层共享类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from firstcoder.providers.types import ToolDefinition


@dataclass(slots=True)
class ToolResult:
    """一次工具执行后的结构化结果。

    agent 后续会把这个结果转换成 `role="tool"` 的消息，再发回模型。
    `content` 是给模型看的主要文本，`data` 保留结构化信息，方便 UI 展示。
    """

    name: str
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ToolExecutor(Protocol):
    """工具执行函数的协议。

    每个工具最终都是一个接收关键字参数、返回 `ToolResult` 的函数。
    """

    def __call__(self, **kwargs: Any) -> ToolResult:
        ...


@dataclass(slots=True)
class Tool:
    """一个完整工具由模型可见 schema 和本地 executor 组成。"""

    definition: ToolDefinition
    executor: ToolExecutor

    @property
    def name(self) -> str:
        return self.definition.name


def make_error_result(name: str, message: str, **data: Any) -> ToolResult:
    """创建统一的失败结果，避免各工具重复拼接错误结构。"""

    return ToolResult(name=name, ok=False, content=message, data=data, error=message)


def make_text_result(name: str, content: str, **data: Any) -> ToolResult:
    """创建统一的成功文本结果。"""

    return ToolResult(name=name, ok=True, content=content, data=data)
