"""`glob` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox


def create_glob_tool(root: str | Path) -> Tool:
    """创建路径匹配工具。"""

    sandbox = PathSandbox(root)

    def glob(pattern: str, path: str = ".", max_results: int = 200) -> ToolResult:
        """按 glob pattern 查找项目目录内的文件和文件夹路径。"""

        try:
            target = sandbox.resolve_validated(path, expect="dir")
        except ValueError as exc:
            return make_error_result("glob", str(exc))
        if max_results <= 0:
            return make_error_result("glob", "max_results 必须大于 0")

        all_matches = sorted(sandbox.relative(item) for item in target.glob(pattern))
        matches = all_matches[:max_results]
        content = "\n".join(matches) if matches else "没有找到匹配路径。"
        return make_text_result("glob", content, matches=matches, truncated=len(all_matches) > max_results)

    return tool_from_function(glob)
