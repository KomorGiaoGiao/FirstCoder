"""同步 FirstCoder 与异步 MCP SDK 之间的连接协调器。"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import replace
from typing import Coroutine, Literal, Mapping

from firstcoder.mcp.config import resolve_environment_placeholders
from firstcoder.mcp.models import McpConfigError, McpLocalServerConfig, McpRemoteServerConfig, McpServerStatus, McpToolDescription
from firstcoder.mcp.transport import McpTransport, McpTransportFactory, SdkMcpTransportFactory


McpServerConfig = McpLocalServerConfig | McpRemoteServerConfig


class McpManager:
    """在守护线程中维护 MCP 连接，并提供同步调用入口。"""

    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        transport_factory: McpTransportFactory | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._configs = {config.name: config for config in configs}
        self._factory = transport_factory or SdkMcpTransportFactory()
        self._environment = os.environ if environment is None else environment
        self._lock = threading.RLock()
        self._statuses = {
            config.name: McpServerStatus(config.name, "disabled" if not config.enabled else "failed")
            for config in configs
        }
        self._transports: dict[str, McpTransport] = {}
        self._catalogs: dict[str, tuple[McpToolDescription, ...]] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="firstcoder-mcp", daemon=True)
        self._thread.start()
        self._closed = False

    def connect_all(self) -> None:
        """连接所有启用服务器；任何单个失败都只影响自身状态。"""

        for config in self._configs.values():
            if not config.enabled:
                self._set_status(config.name, "disabled")
                continue
            self._connect_one(config)

    def statuses(self) -> tuple[McpServerStatus, ...]:
        """返回所有服务器的安全状态快照。"""

        with self._lock:
            return tuple(self._statuses[name] for name in self._configs)

    def doctor(self, name: str) -> McpServerStatus | None:
        """返回单个服务器状态；未知名称返回 ``None``。"""

        with self._lock:
            return self._statuses.get(name)

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]:
        """返回已连接服务器可用的工具目录。"""

        with self._lock:
            return tuple((name, tool) for name in self._configs for tool in self._catalogs.get(name, ()))

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object:
        """同步调用已发现的 MCP 工具。"""

        with self._lock:
            config = self._configs.get(server)
            transport = self._transports.get(server)
            catalog = self._catalogs.get(server, ())
        if config is None or transport is None or not any(item.name == tool for item in catalog):
            raise RuntimeError("MCP 工具不可用")
        try:
            return self._submit(transport.call_tool(tool, arguments), config.timeout_ms)
        except FutureTimeoutError as error:
            raise RuntimeError("MCP 请求超时") from error
        except Exception as error:
            raise RuntimeError("MCP 工具调用失败") from error

    def close(self) -> None:
        """断开所有连接并停止后台事件循环，可重复调用。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            transports = tuple(self._transports.items())
            self._transports.clear()
            self._catalogs.clear()
        for name, transport in transports:
            try:
                self._submit(transport.close(), 1000)
            except Exception:
                pass
            self._set_status(name, "failed", error="MCP 已断开")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1)

    def _connect_one(self, config: McpServerConfig) -> None:
        self._set_status(config.name, "connecting")
        try:
            resolved = self._resolve_config(config)
            transport = self._factory.create(resolved)
            tools = self._submit(self._initialize(transport), config.timeout_ms)
        except McpConfigError:
            self._set_status(config.name, "failed", error="MCP 配置无效")
            return
        except FutureTimeoutError:
            self._set_status(config.name, "failed", error="MCP 请求超时")
            return
        except Exception:
            self._set_status(config.name, "failed", error="MCP 连接失败")
            return
        with self._lock:
            self._transports[config.name] = transport
            filtered_tools = self._allowed_tools(config, tools)
            self._catalogs[config.name] = filtered_tools
        self._set_status(config.name, "connected", tool_count=len(filtered_tools))

    async def _initialize(self, transport: McpTransport) -> tuple[McpToolDescription, ...]:
        try:
            await transport.connect()
            return await transport.list_tools()
        except BaseException:
            await transport.close()
            raise

    def _resolve_config(self, config: McpServerConfig) -> McpServerConfig:
        if isinstance(config, McpLocalServerConfig):
            return replace(
                config,
                command=tuple(resolve_environment_placeholders(config.command, self._environment)),
                env=resolve_environment_placeholders(config.env, self._environment),
            )
        return replace(
            config,
            url=resolve_environment_placeholders(config.url, self._environment),
            headers=resolve_environment_placeholders(config.headers, self._environment),
        )

    def _set_status(
        self,
        name: str,
        state: Literal["disabled", "connecting", "connected", "failed"],
        tool_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._statuses[name] = McpServerStatus(name, state, tool_count, error)

    def _submit(self, coroutine: Coroutine[object, object, object], timeout_ms: int) -> object:
        future: Future[object] = asyncio.run_coroutine_threadsafe(
            self._with_timeout(coroutine, timeout_ms), self._loop
        )
        try:
            return future.result(timeout=timeout_ms / 1000 + 0.2)
        except (FutureTimeoutError, TimeoutError) as error:
            future.cancel()
            raise FutureTimeoutError from error

    async def _with_timeout(
        self, coroutine: Coroutine[object, object, object], timeout_ms: int
    ) -> object:
        return await asyncio.wait_for(coroutine, timeout=timeout_ms / 1000)

    @staticmethod
    def _allowed_tools(
        config: McpServerConfig, tools: tuple[McpToolDescription, ...]
    ) -> tuple[McpToolDescription, ...]:
        if config.allowed_tools is None:
            return tools
        return tuple(
            tool
            for tool in tools
            if any(fnmatch.fnmatchcase(tool.name, pattern) for pattern in config.allowed_tools)
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
