"""`read_multi` 工具。

批量读取多个文件，减少模型反复调用 view 的往返开销。
"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.path_permissions import read_multi_target, with_read_permission
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess
from firstcoder.utils.text import safe_read_text


def create_read_multi_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建批量文件读取工具。"""

    sandbox = PathSandbox(root, access=access)

    def read_multi(paths: list[str], max_total_chars: int = 100000) -> ToolResult:
        """批量读取项目内 UTF-8 文本文件；总输出受限。"""

        if not paths:
            return make_error_result("read_multi", "paths 不能为空列表")
        if max_total_chars <= 0:
            return make_error_result("read_multi", "max_total_chars 必须大于 0")

        contents: list[str] = []
        errors: list[str] = []
        file_data: list[dict[str, object]] = []
        total_chars = 0
        truncated = False

        for path in paths:
            try:
                target = sandbox.resolve(path)
            except ValueError as exc:
                errors.append(f"{path}: {exc}")
                continue

            if not target.exists():
                errors.append(f"{path}: 文件不存在")
                continue
            if not target.is_file():
                errors.append(f"{path}: 路径不是文件")
                continue

            try:
                text = safe_read_text(target)
            except UnicodeDecodeError:
                errors.append(f"{path}: 文件不是 UTF-8 文本")
                continue

            relative = sandbox.relative(target)
            file_header = f"=== {relative} ===\n"
            file_text = file_header + text + "\n"

            # 检查总长度限制
            if total_chars + len(file_text) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining > len(file_header):
                    contents.append(file_header)
                    contents.append(text[: remaining - len(file_header) - len("\n")])
                    contents.append("\n")
                contents.append("\n[已截断：超出 max_total_chars 限制]\n")
                truncated = True
                break

            contents.append(file_text)
            total_chars += len(file_text)
            file_data.append({"path": relative, "lines": text.count("\n") + 1})

        content = "".join(contents).rstrip("\n")

        if errors:
            # 如果有错误，整体标记为失败，但成功读取的文件内容也保留
            error_summary = "\n".join(f"- {error}" for error in errors)
            full_content = f"{content}\n\n[读取错误]\n{error_summary}".lstrip("\n")
            return make_error_result(
                "read_multi",
                full_content,
                files=file_data,
                errors=errors,
                truncated=truncated,
            )

        return make_text_result(
            "read_multi",
            content,
            files=file_data,
            truncated=truncated,
        )

    return with_read_permission(
        tool_from_function(read_multi),
        reason="批量读取文件需要权限检查。",
        target_builder=read_multi_target,
    )
