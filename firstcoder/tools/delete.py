"""`delete` 工具。"""

from __future__ import annotations

import shutil
from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_delete_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建删除文件或目录的工具。"""

    sandbox = PathSandbox(root, access=access)

    def delete(path: str, recursive: bool = False) -> ToolResult:
        """删除项目内文件或目录；目录删除必须 recursive=true。"""

        try:
            target = _resolve_delete_target(sandbox, path)
        except ValueError as exc:
            return make_error_result("delete", str(exc))
        if target.resolve() == sandbox.root:
            return make_error_result("delete", "不能删除项目根目录")

        relative = sandbox.relative(target)
        if target.is_dir() and not target.is_symlink():
            if not recursive:
                return make_error_result("delete", "删除目录必须启用 recursive")
            shutil.rmtree(target)
            return make_text_result("delete", f"已删除目录：{relative}", path=relative, type="dir")

        target.unlink()
        return make_text_result("delete", f"已删除文件：{relative}", path=relative, type="file")

    tool = tool_from_function(delete)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.DELETE_PATH,
        target_arg="path",
        reason="删除路径需要用户确认。",
    )
    return tool


def _resolve_delete_target(sandbox: PathSandbox, path: str | Path | None) -> Path:
    """解析删除目标，但保留符号链接本身用于 unlink。"""

    lexical = sandbox.root if path in (None, "") else sandbox.root / Path(path)
    resolved = lexical.resolve()
    if resolved != sandbox.root and sandbox.root not in resolved.parents:
        raise ValueError(f"路径超出项目目录：{path}")
    if not lexical.exists() and not lexical.is_symlink():
        raise ValueError(f"路径不存在：{path}")
    return lexical
