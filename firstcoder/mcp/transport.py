"""基于官方 MCP SDK 的传输适配层。"""

from __future__ import annotations

from contextlib import AsyncExitStack
import os
from typing import Any, Mapping, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from firstcoder.mcp.models import McpLocalServerConfig, McpRemoteServerConfig, McpToolDescription


def _stdio_environment(config_environment: Mapping[str, str]) -> dict[str, str]:
    """保留宿主进程环境，并用 MCP 服务端配置覆盖指定变量。"""

    environment = dict(os.environ)
    environment.update(config_environment)
    return environment


class McpTransport(Protocol):
    """管理器所需的最小异步 MCP 传输接口。"""

    async def connect(self) -> None: ...

    async def list_tools(self) -> tuple[McpToolDescription, ...]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...

    async def close(self) -> None: ...


class McpTransportFactory(Protocol):
    """按服务器配置创建传输，便于在测试中注入替身。"""

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> McpTransport: ...


class SdkMcpTransportFactory:
    """创建官方 SDK 支持的 stdio 或 Streamable HTTP 客户端。"""

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> McpTransport:
        if isinstance(config, McpLocalServerConfig):
            return _StdioMcpTransport(config)
        return _StreamableHttpMcpTransport(config)


class _SdkMcpTransport:
    """复用 SDK 会话生命周期的内部基类。"""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await self._open_streams(stack)
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session

    async def list_tools(self) -> tuple[McpToolDescription, ...]:
        session = self._require_session()
        result = await session.list_tools()
        return tuple(
            McpToolDescription(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.inputSchema),
            )
            for tool in result.tools
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return await self._require_session().call_tool(name, arguments)

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            self._session = None
            await stack.aclose()

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP 传输尚未连接")
        return self._session

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        raise NotImplementedError


class _StdioMcpTransport(_SdkMcpTransport):
    """本地 stdio 服务端传输；SDK 直接接收 argv，不经过 shell。"""

    def __init__(self, config: McpLocalServerConfig) -> None:
        super().__init__()
        self._config = config

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        parameters = StdioServerParameters(
            command=self._config.command[0],
            args=list(self._config.command[1:]),
            env=_stdio_environment(self._config.env),
        )
        return await stack.enter_async_context(stdio_client(parameters))


class _StreamableHttpMcpTransport(_SdkMcpTransport):
    """远程 Streamable HTTP 服务端传输。"""

    def __init__(self, config: McpRemoteServerConfig) -> None:
        super().__init__()
        self._config = config

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(self._config.url, headers=dict(self._config.headers))
        )
        return read_stream, write_stream
