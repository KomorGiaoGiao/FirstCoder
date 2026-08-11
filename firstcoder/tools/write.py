"""`write` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.file_feedback import format_change_content, render_text_diff
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_write_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建写入文本文件的工具。"""

    sandbox = PathSandbox(root, access=access)

    def write(path: str, content: str, create_dirs: bool = True, overwrite: bool = True) -> ToolResult:
        """写入项目内 UTF-8 文本文件；可创建目录或覆盖文件。"""

        target = sandbox.resolve(path)
        if target.exists() and target.is_dir():
            return make_error_result("write", f"路径是目录，不能写入文件：{path}")
        if target.exists() and not overwrite:
            return make_error_result("write", f"文件已存在且 overwrite 为 False：{path}")

        parent = target.parent
        if not parent.exists():
            if not create_dirs:
                return make_error_result("write", f"父目录不存在：{sandbox.relative(parent)}")
            parent.mkdir(parents=True, exist_ok=True)

        created = not target.exists()
        before: str | None = None
        diff_unavailable = False
        if not created:
            try:
                before = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                diff_unavailable = True
        changed = created or before != content or diff_unavailable
        relative = sandbox.relative(target)
        if changed:
            target.write_text(content, encoding="utf-8")
        diff = ""
        diff_truncated = False
        if changed and not diff_unavailable:
            diff, diff_truncated = render_text_diff(relative, before, content)
        summary = f"已写入文件：{relative}"
        if diff_unavailable:
            summary += "（原内容不是 UTF-8 文本，无法生成逐行 diff）"
        return make_text_result(
            "write",
            format_change_content(summary, diff, no_op=not changed),
            path=relative,
            bytes_written=len(content.encode("utf-8")),
            created=created,
            changed=changed,
            no_op=not changed,
            diff=diff,
            diff_truncated=diff_truncated,
            diff_unavailable=diff_unavailable,
        )

    tool = tool_from_function(write)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.WRITE_PATH,
        target_arg="path",
        reason="写入文件需要用户确认。",
    )
    return tool
