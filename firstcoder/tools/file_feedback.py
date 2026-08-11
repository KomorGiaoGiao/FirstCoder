"""文件变更工具共享的模型可见 diff 与 no-op 反馈。"""

from __future__ import annotations

from difflib import unified_diff

from firstcoder.utils.text import truncate_head_tail

DEFAULT_DIFF_MAX_CHARS = 12000


def render_text_diff(
    path: str,
    before: str | None,
    after: str | None,
    *,
    source_path: str | None = None,
    max_chars: int = DEFAULT_DIFF_MAX_CHARS,
) -> tuple[str, bool]:
    """生成有界 unified diff，并保留头部文件名和尾部错误上下文。"""

    lines = unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        fromfile=f"a/{source_path or path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return truncate_head_tail("\n".join(lines), max_chars)


def format_change_content(summary: str, diff: str, *, no_op: bool) -> str:
    """把动作摘要和 diff/no-op 结论组合成紧凑工具结果。"""

    if no_op:
        return f"{summary} 内容未变化；未产生文件变化。"
    if not diff:
        return summary
    return f"{summary}\n\nDiff:\n{diff}"
