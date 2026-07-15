"""MCP 配置与运行状态的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping


class McpConfigError(ValueError):
    """表示不会暴露敏感配置值的 MCP 配置错误。"""


@dataclass(frozen=True, slots=True)
class McpLocalServerConfig:
    """本地 stdio MCP 服务器的期望配置。"""

    name: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_ms: int = 5000
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, slots=True)
class McpRemoteServerConfig:
    """远程 Streamable HTTP MCP 服务器的期望配置。"""

    name: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_ms: int = 5000
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    """进程内 MCP 服务器连接状态，不是持久化配置。"""

    name: str
    state: Literal["disabled", "connecting", "connected", "failed"]
    tool_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class McpToolDescription:
    """MCP 服务端发现的、与传输无关的工具说明。"""

    name: str
    description: str | None
    input_schema: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
