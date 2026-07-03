"""`ls` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_ls_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建列出目录内容的工具。"""

    sandbox = PathSandbox(root, access=access)

    def ls(path: str = ".", recursive: bool = False, max_entries: int = 200) -> ToolResult:
        """列出项目内目录项；只返回名称和文件/目录类型。"""

        try:
            target = sandbox.resolve_validated(path, expect="dir")
        except ValueError as exc:
            return make_error_result("ls", str(exc))
        if max_entries <= 0:
            return make_error_result("ls", "max_entries 必须大于 0")

        pattern = "**/*" if recursive else "*"
        entries = []
        items = sorted(target.glob(pattern), key=lambda item: sandbox.relative(item))
        for item in items:
            if len(entries) >= max_entries:
                break
            relative = sandbox.relative(item)
            entries.append({"path": relative, "type": "dir" if item.is_dir() else "file"})

        lines = [f"{entry['type']}\t{entry['path']}" for entry in entries]
        content = "\n".join(lines) if lines else "目录为空。"
        return make_text_result("ls", content, entries=entries, truncated=len(entries) >= max_entries)

    return tool_from_function(ls)
