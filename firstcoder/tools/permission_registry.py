"""权限感知工具注册表 wrapper。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.types import PermissionDecisionKind, PermissionRequest
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.permission_results import make_permission_confirmation_result, make_permission_denied_result
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result


class PermissionAwareToolRegistry:
    """在工具执行前统一做权限预检。

    这一层只包住 `ToolRegistry`，不改变模型可见工具 schema。老工具如果没有
    `ToolPermissionSpec`，会按原逻辑直接执行。
    """

    def __init__(self, registry: ToolRegistry, permission_manager: PermissionManager) -> None:
        self.registry = registry
        self.permission_manager = permission_manager

    def register(self, tool: Tool) -> None:
        self.registry.register(tool)

    def definitions(self) -> list[ToolDefinition]:
        return self.registry.definitions()

    def names(self) -> list[str]:
        return self.registry.names()

    def tools(self) -> list[Tool]:
        return self.registry.tools()

    def get(self, name: str) -> Tool | None:
        """按名称返回工具对象。"""

        return self.registry.get(name)

    def execute(self, name: str, arguments: dict[str, Any] | str | None = None) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return self.registry.execute(name, arguments)

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self.registry.execute(name, arguments)

        if tool.permission is None:
            return self.registry.execute(name, arguments)

        try:
            request = permission_request_for_tool(tool, arguments)
        except ValueError as exc:
            return make_error_result(name, str(exc), arguments=arguments)
        request = self.permission_manager.normalize_request(request)
        decision = self.permission_manager.preflight(request)
        if decision.kind == PermissionDecisionKind.DENY:
            return make_permission_denied_result(tool_name=name, request=request, decision=decision)
        if decision.kind == PermissionDecisionKind.ASK:
            confirmation = self.permission_manager.build_confirmation(request)
            return make_permission_confirmation_result(
                tool_name=name,
                request=request,
                confirmation=confirmation,
            )
        return self.registry.execute(name, arguments)


def permission_request_for_tool(tool: Tool, arguments: dict[str, Any]) -> PermissionRequest:
    """根据工具声明和调用参数构造权限请求。"""

    spec = tool.permission
    if spec is None:
        raise ValueError(f"工具没有权限声明：{tool.name}")

    target = _target_from_arguments(spec, arguments)
    cwd = _cwd_from_arguments(spec, arguments)
    request_id = _permission_request_id(tool.name, arguments)
    return PermissionRequest(
        id=request_id,
        action=spec.action,
        target=target,
        reason=spec.reason or f"工具 {tool.name} 请求 {spec.action.value} 权限。",
        cwd=cwd,
        metadata={
            "tool_name": tool.name,
            "arguments": dict(arguments),
            "allow_always": spec.allow_always,
            "allow_auto": spec.allow_auto,
        },
    )


def _target_from_arguments(spec: ToolPermissionSpec, arguments: dict[str, Any]) -> str:
    if spec.target_value is not None:
        return spec.target_value
    if spec.target_arg is None:
        return ""
    if spec.target_arg not in arguments:
        raise ValueError(f"权限声明缺少目标参数：{spec.target_arg}")
    return str(arguments[spec.target_arg])


def _cwd_from_arguments(spec: ToolPermissionSpec, arguments: dict[str, Any]) -> Path | None:
    if spec.cwd_arg is None:
        return None
    raw = arguments.get(spec.cwd_arg)
    if raw in (None, ""):
        return None
    return Path(str(raw))


def _permission_request_id(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return f"perm_{tool_name}_{sha256(payload.encode('utf-8')).hexdigest()[:12]}"
