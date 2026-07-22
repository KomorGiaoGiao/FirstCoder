"""Shared permission helpers for path-reading tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.types import Tool, ToolPermissionSpec


def read_path_target(arguments: dict[str, Any]) -> str:
    return str(arguments.get("path") or ".")


def read_multi_target(arguments: dict[str, Any]) -> str:
    paths = arguments.get("paths")
    if not isinstance(paths, list):
        return ""
    return "\n".join(str(path) for path in paths)


def with_read_permission(
    tool: Tool,
    *,
    reason: str,
    target_builder: Callable[[dict[str, Any]], str] = read_path_target,
) -> Tool:
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.READ_PATH,
        target_builder=target_builder,
        reason=reason,
    )
    return tool
