"""命令类工具共享的模型可见结果格式化。"""

from __future__ import annotations

from collections.abc import Mapping

from firstcoder.tools.types import ToolResult, make_text_result
from firstcoder.utils.subprocess import CommandResult


def command_tool_result(
    name: str,
    result: CommandResult,
    *,
    data: Mapping[str, object],
    nonzero_error: str,
    success_fallback: str,
) -> ToolResult:
    """把统一子进程结果转换为模型可诊断的 ToolResult。"""

    payload = dict(data)
    if result.error:
        return _failure_result(name, result, error=result.error, data=payload)
    if not result.ok:
        return _failure_result(name, result, error=nonzero_error, data=payload)

    content = result.stdout.strip() or result.stderr.strip() or success_fallback
    return make_text_result(name, content, **payload)


def _failure_result(
    name: str,
    result: CommandResult,
    *,
    error: str,
    data: dict[str, object],
) -> ToolResult:
    sections = [error]
    if result.stdout:
        sections.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        sections.append(f"stderr:\n{result.stderr.rstrip()}")
    return ToolResult(
        name=name,
        ok=False,
        content="\n\n".join(sections),
        data=data,
        error=error,
    )
