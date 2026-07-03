"""`tree` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_tree_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建目录树查看工具。"""

    sandbox = PathSandbox(root, access=access)

    def tree(path: str = ".", max_depth: int = 3, max_entries: int = 200) -> ToolResult:
        """展示项目内目录树；适合快速了解结构。"""

        try:
            target = sandbox.resolve_validated(path, expect="dir")
        except ValueError as exc:
            return make_error_result("tree", str(exc))
        if max_depth <= 0:
            return make_error_result("tree", "max_depth 必须大于 0")
        if max_entries <= 0:
            return make_error_result("tree", "max_entries 必须大于 0")

        lines: list[str] = []
        entries: list[str] = []
        truncated = _walk_tree(sandbox, target, target, lines, entries, 0, max_depth, max_entries)
        content = "\n".join(lines) if lines else "目录为空。"
        return make_text_result("tree", content, entries=entries, truncated=truncated)

    return tool_from_function(tree)


def _walk_tree(
    sandbox: PathSandbox,
    root: Path,
    current: Path,
    lines: list[str],
    entries: list[str],
    depth: int,
    max_depth: int,
    max_entries: int,
) -> bool:
    """递归构造目录树文本。"""

    if depth >= max_depth:
        return False

    children = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    truncated = False
    for child in children:
        if len(entries) >= max_entries:
            return True

        relative = sandbox.relative(child)
        display = f"{relative}/" if child.is_dir() else relative
        lines.append(f"{'  ' * depth}{display}")
        entries.append(display)

        if child.is_dir():
            truncated = _walk_tree(sandbox, root, child, lines, entries, depth + 1, max_depth, max_entries) or truncated
            if truncated:
                return True
    return truncated
