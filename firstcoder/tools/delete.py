"""`delete` 工具。"""

from __future__ import annotations

import shutil
from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox


def create_delete_tool(root: str | Path) -> Tool:
    """创建删除文件或目录的工具。"""

    sandbox = PathSandbox(root)

    def delete(path: str, recursive: bool = False) -> ToolResult:
        """删除项目目录内的文件或目录。"""

        try:
            target = sandbox.resolve_validated(path)
        except ValueError as exc:
            return make_error_result("delete", str(exc))
        if target == sandbox.root:
            return make_error_result("delete", "不能删除项目根目录")

        relative = sandbox.relative(target)
        if target.is_dir():
            if not recursive:
                return make_error_result("delete", "删除目录必须启用 recursive")
            shutil.rmtree(target)
            return make_text_result("delete", f"已删除目录：{relative}", path=relative, type="dir")

        target.unlink()
        return make_text_result("delete", f"已删除文件：{relative}", path=relative, type="file")

    return tool_from_function(delete)
