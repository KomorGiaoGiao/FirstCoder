"""`view` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.text import safe_read_text


def create_view_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建读取文本文件的工具。"""

    sandbox = PathSandbox(root, access=access)

    def view(path: str, offset: int = 0, limit: int = 200) -> ToolResult:
        """按行读取项目内 UTF-8 文本文件；支持分页。"""

        try:
            target = sandbox.resolve_validated(path, expect="file")
        except ValueError as exc:
            return make_error_result("view", str(exc))
        if offset < 0:
            return make_error_result("view", "offset 不能小于 0")
        if limit <= 0:
            return make_error_result("view", "limit 必须大于 0")

        try:
            lines = safe_read_text(target).splitlines()
        except UnicodeDecodeError:
            return make_error_result("view", f"文件不是 UTF-8 文本或无法作为文本读取：{path}")

        selected = lines[offset : offset + limit]
        start_line = offset + 1 if selected else None
        end_line = offset + len(selected) if selected else None
        content = "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected, start=offset + 1))
        truncated = offset + limit < len(lines)

        return make_text_result(
            "view",
            content or "没有可显示内容。",
            path=sandbox.relative(target),
            start_line=start_line,
            end_line=end_line,
            truncated=truncated,
            total_lines=len(lines),
        )

    return tool_from_function(view)
