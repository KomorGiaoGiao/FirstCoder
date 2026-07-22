"""MCP 管理器的连接生命周期测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

from firstcoder.mcp.manager import McpManager
from firstcoder.mcp.models import McpLocalServerConfig, McpToolDescription
from firstcoder.mcp.transport import _stdio_environment


@dataclass
class FakeTransport:
    """不依赖真实 MCP 服务端的传输替身。"""

    tools: tuple[McpToolDescription, ...] = ()
    connect_error: Exception | None = None
    list_error: Exception | None = None
    delay: float = 0
    fail_connect_attempts: int = 0
    connected: bool = False
    closed: bool = False
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    connect_calls: int = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.connect_calls <= self.fail_connect_attempts:
            raise RuntimeError("temporary connection failure")
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def list_tools(self) -> tuple[McpToolDescription, ...]:
        if self.list_error:
            raise self.list_error
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        return {"name": name, "arguments": arguments}

    async def close(self) -> None:
        self.closed = True


class FakeTransportFactory:
    def __init__(self, transports: dict[str, FakeTransport]) -> None:
        self.transports = transports

    def create(self, config: McpLocalServerConfig) -> FakeTransport:
        return self.transports[config.name]


def server(name: str, **kwargs: object) -> McpLocalServerConfig:
    return McpLocalServerConfig(name=name, command=("fake",), **kwargs)


def test_connect_all_marks_disabled_server_without_creating_transport():
    factory = FakeTransportFactory({"disabled": FakeTransport()})
    manager = McpManager((server("disabled", enabled=False),), factory)

    manager.connect_all()

    assert manager.statuses() == (manager.doctor("disabled"),)
    assert manager.doctor("disabled").state == "disabled"
    assert manager.tools() == ()
    manager.close()


def test_connect_all_discovers_tools_and_call_tool_uses_connected_transport():
    discovered = McpToolDescription("calendar_list", "列出日程", {"type": "object"})
    transport = FakeTransport(tools=(discovered,))
    manager = McpManager((server("lark"),), FakeTransportFactory({"lark": transport}))

    manager.connect_all()

    assert manager.doctor("lark").state == "connected"
    assert manager.doctor("lark").tool_count == 1
    assert manager.tools() == (("lark", discovered),)
    assert manager.call_tool("lark", "calendar_list", {"limit": 3}) == {
        "name": "calendar_list",
        "arguments": {"limit": 3},
    }
    manager.close()
    assert transport.closed is True


def test_connect_all_filters_discovered_tools_using_allowed_tools_patterns():
    transport = FakeTransport(tools=(McpToolDescription("calendar_list", None), McpToolDescription("doc_read", None)))
    manager = McpManager(
        (server("lark", allowed_tools=("calendar_*",)),),
        FakeTransportFactory({"lark": transport}),
    )

    manager.connect_all()

    assert manager.tools() == (("lark", McpToolDescription("calendar_list", None)),)
    assert manager.doctor("lark").tool_count == 1
    manager.close()


def test_connect_all_keeps_other_servers_available_after_initialization_failure():
    broken = FakeTransport(list_error=RuntimeError("Bearer secret-value"))
    healthy = FakeTransport(tools=(McpToolDescription("echo", None),))
    manager = McpManager(
        (server("broken"), server("healthy")),
        FakeTransportFactory({"broken": broken, "healthy": healthy}),
    )

    manager.connect_all()

    failed = manager.doctor("broken")
    assert failed.state == "failed"
    assert failed.error == "MCP 连接失败"
    assert "secret-value" not in (failed.error or "")
    assert manager.doctor("healthy").state == "connected"
    manager.close()


def test_connect_all_marks_timeout_as_failed_and_does_not_raise():
    slow = FakeTransport(delay=0.2)
    manager = McpManager((server("slow", timeout_ms=20),), FakeTransportFactory({"slow": slow}), retry_delay_seconds=0)

    started = time.monotonic()
    manager.connect_all()

    assert time.monotonic() - started < 0.15
    assert manager.doctor("slow").state == "failed"
    assert manager.doctor("slow").error == "MCP 请求超时"
    manager.close()


def test_connect_all_exposes_connecting_state_while_connection_is_in_progress():
    transport = FakeTransport(delay=0.1)
    manager = McpManager((server("slow"),), FakeTransportFactory({"slow": transport}))

    import threading

    worker = threading.Thread(target=manager.connect_all)
    worker.start()
    seen_connecting = False
    for _ in range(50):
        if manager.doctor("slow").state == "connecting":
            seen_connecting = True
            break
        time.sleep(0.005)
    worker.join()

    assert seen_connecting is True
    assert manager.doctor("slow").state == "connected"
    manager.close()


def test_disconnect_marks_connected_servers_failed_and_removes_tools():
    transport = FakeTransport(tools=(McpToolDescription("echo", None),))
    manager = McpManager((server("echo"),), FakeTransportFactory({"echo": transport}))
    manager.connect_all()

    manager.close()

    assert transport.closed is True
    assert manager.doctor("echo").state == "failed"
    assert manager.doctor("echo").error == "MCP 已断开"
    assert manager.tools() == ()


def test_stdio_environment_inherits_process_environment_and_overlays_server_values(monkeypatch):
    monkeypatch.setenv("PATH", "/host/bin")
    monkeypatch.setenv("INHERITED", "host-value")

    environment = _stdio_environment({"PATH": "/server/bin", "CONFIGURED": "server-value"})

    assert environment["PATH"] == "/server/bin"
    assert environment["INHERITED"] == "host-value"
    assert environment["CONFIGURED"] == "server-value"


def test_close_is_idempotent_and_background_thread_closes_its_own_loop(monkeypatch):
    manager = McpManager(())
    close_threads: list[int] = []
    loop_close = manager._loop.close

    def close_loop() -> None:
        close_threads.append(threading.get_ident())
        loop_close()

    monkeypatch.setattr(manager._loop, "close", close_loop)

    manager.close()
    manager.close()

    assert manager._thread.is_alive() is False
    assert close_threads == [manager._thread.ident]


def test_connect_all_retries_a_temporary_failure_three_times_without_blocking_other_servers():
    retrying = FakeTransport(fail_connect_attempts=2, tools=(McpToolDescription("echo", None),))
    healthy = FakeTransport(tools=(McpToolDescription("ping", None),))
    manager = McpManager(
        (server("retrying"), server("healthy")),
        FakeTransportFactory({"retrying": retrying, "healthy": healthy}),
        retry_delay_seconds=0,
    )

    manager.connect_all()

    assert retrying.connect_calls == 3
    assert manager.doctor("retrying").state == "connected"
    assert manager.doctor("healthy").state == "connected"
    manager.close()


def test_connect_all_in_background_returns_before_slow_server_finishes():
    slow = FakeTransport(delay=0.2, tools=(McpToolDescription("echo", None),))
    manager = McpManager(
        (server("slow"),),
        FakeTransportFactory({"slow": slow}),
        retry_delay_seconds=0,
    )

    started = time.monotonic()
    manager.connect_all_in_background()

    assert time.monotonic() - started < 0.05
    assert manager.doctor("slow").state == "connecting"
    assert manager.wait_for_connections(timeout=1) is True
    assert manager.doctor("slow").state == "connected"
    manager.close()


def test_reconnect_replaces_a_connected_server_in_the_background():
    transport = FakeTransport(tools=(McpToolDescription("echo", None),))
    manager = McpManager((server("echo"),), FakeTransportFactory({"echo": transport}))
    manager.connect_all()

    assert manager.reconnect("echo") is True
    for _ in range(50):
        if manager.doctor("echo").state == "connected" and transport.connect_calls == 2:
            break
        time.sleep(0.01)

    assert transport.closed is True
    assert transport.connect_calls == 2
    assert manager.doctor("echo").state == "connected"
    assert manager.reconnect("missing") is False
    manager.close()
