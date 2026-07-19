"""Activity-line, tool-event, and todo rendering helpers."""

from __future__ import annotations

from rich.markup import escape

from firstcoder.app.tui_state import TuiTodoItem


def activity_markup(text: str) -> str:
    color = "#7bba55"
    if text.startswith("waiting"):
        color = "#b28443"
    elif text.startswith("running"):
        color = "#808185"
    elif text.startswith("streaming"):
        color = "#6e6d72"
    elif text.startswith("error"):
        color = "#c85f5f"
    return f"[{color}]{escape(text)}[/]"


def truncate_activity_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def turn_metrics_text(elapsed_seconds: float, tool_count: int) -> str:
    elapsed = format_elapsed_time(elapsed_seconds)
    return f"{elapsed} · {tool_count} {'tool' if tool_count == 1 else 'tools'}"


def format_elapsed_time(elapsed_seconds: float) -> str:
    if elapsed_seconds < 60:
        return f"{max(0.0, elapsed_seconds):.1f}s"
    total_seconds = int(max(0, elapsed_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def tool_event_status(event) -> str | None:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "prewrite_review":
        return "review"
    if kind == "started":
        return "running"
    if kind == "finished":
        result = getattr(event, "result", None)
        return "success" if getattr(result, "ok", False) else "error"
    if kind == "permission_requested":
        return "permission_requested"
    if kind == "denied":
        return "denied"
    if kind == "skipped":
        return "skipped"
    return None


def tool_event_label(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    status = tool_event_status(event)
    if status == "permission_requested":
        return "permission requested"
    return f"tool {name} {status}" if status else f"tool {name}"


def tool_activity_summary(event) -> str:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        tool_call = getattr(event, "tool_call", None)
        return compact_tool_arguments(getattr(tool_call, "arguments", None))
    if kind == "finished":
        result = getattr(event, "result", None)
        return compact_tool_content(str(getattr(result, "content", "") or ""))
    return ""


def tool_activity_line_text(name: str, status: str) -> str:
    if status == "running":
        return f"running · {name}"
    if status == "success":
        return post_tool_reasoning_text(name)
    if status == "permission_requested":
        return "waiting · permission"
    if status in {"error", "failed"}:
        return f"error · {name}"
    return f"{status} · {name}"


def post_tool_reasoning_text(name: str) -> str:
    return f"reading {name} result"


def todo_panel_text(todos: list[TuiTodoItem]) -> str:
    lines = ["Todo · model reported"]
    for item in todos:
        marker = "[ ]"
        if item.status in {"completed", "done"}:
            marker = "[✓]"
        elif item.status == "in_progress":
            marker = "[~]"
        elif item.status == "cancelled":
            marker = "[-]"
        lines.append(f"{marker} {item.content}")
    return "\n".join(lines)


def tool_status_text(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    kind = str(getattr(event, "kind", "") or "")
    if kind == "prewrite_review":
        return ""
    if kind == "started":
        arguments = compact_tool_arguments(getattr(tool_call, "arguments", None))
        suffix = f" {arguments}" if arguments else ""
        return f"正在调用工具：{name}{suffix}"
    if kind == "finished":
        result = getattr(event, "result", None)
        status = "完成" if getattr(result, "ok", False) else "失败"
        content = compact_tool_content(str(getattr(result, "content", "") or ""))
        suffix = f"：{content}" if content else ""
        return f"工具{status}：{name}{suffix}"
    if kind == "permission_requested":
        request = getattr(event, "permission_request", None)
        target = str(getattr(request, "target", "") or "")
        action = str(getattr(request, "action", "") or "")
        suffix = f"  {action} {target}".rstrip() if action or target else f"  {name}"
        return f"permission requested{suffix}"
    if kind == "denied":
        return f"工具已拒绝：{name}"
    if kind == "skipped":
        return f"工具已跳过：{name}"
    return ""


def compact_tool_arguments(arguments) -> str:
    if not arguments:
        return ""
    rendered = str(arguments)
    return compact_tool_content(rendered, max_chars=120)


def compact_tool_content(text: str, max_chars: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3] + "..."
