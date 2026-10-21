"""FirstCoder Textual 界面的 UI 状态模型。

这里的状态都是纯 UI 记录(transcript 条目、工具活动、面板渲染簿记), 不是会话事实;
会话事实在 AgentSession 里, 这里只服务于"屏幕上画过什么/正在画什么"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TuiEntryKind(StrEnum):
    """transcript 条目类型, 决定渲染样式与重放时的归类。"""

    SYSTEM = "system"
    COMMAND = "command"
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL = "tool"
    PERMISSION = "permission"
    ERROR = "error"


_DEFAULT_LABELS = {
    TuiEntryKind.SYSTEM: "system",
    TuiEntryKind.COMMAND: "command",
    TuiEntryKind.USER: "you",
    TuiEntryKind.ASSISTANT: "FirstCoder",
    TuiEntryKind.REASONING: "thinking",
    TuiEntryKind.TOOL: "tool",
    TuiEntryKind.PERMISSION: "permission",
    TuiEntryKind.ERROR: "error",
}


@dataclass(slots=True)
class TuiTranscriptEntry:
    # widget 指向已挂载的渲染节点, 供 picker 原地重绘; 测试替身下为 None。
    id: int
    kind: TuiEntryKind
    body: str
    label: str
    status: str | None = None
    widget: Any | None = None


@dataclass(slots=True)
class TuiToolActivity:
    name: str
    status: str
    summary: str = ""


@dataclass(slots=True)
class TuiTaskPlanPanelState:
    """任务面板的渲染簿记: 用 revision 做增量刷新, 避免每个事件都重排面板。"""

    last_rendered_revision: int | None = None


@dataclass(slots=True)
class TuiTranscript:
    """屏幕 transcript 的内存模型: _write_line 先记这里, 再挂载 widget。"""

    entries: list[TuiTranscriptEntry] = field(default_factory=list)
    active_tool: TuiToolActivity | None = None
    recent_tools: list[TuiToolActivity] = field(default_factory=list)
    _next_id: int = 1

    def add(
        self,
        kind: TuiEntryKind,
        body: str,
        *,
        label: str | None = None,
        status: str | None = None,
    ) -> TuiTranscriptEntry:
        entry = TuiTranscriptEntry(
            id=self._next_id,
            kind=kind,
            body=body,
            label=label or _DEFAULT_LABELS[kind],
            status=status,
        )
        self._next_id += 1
        self.entries.append(entry)
        return entry

    def record_tool_activity(self, name: str, status: str, summary: str = "") -> TuiToolActivity:
        """running 覆盖 active_tool(同一时间只高亮一个); 终态归入 recent_tools。"""
        activity = TuiToolActivity(name=name, status=status, summary=summary)
        if status == "running":
            self.active_tool = activity
            return activity
        self.active_tool = None
        self.recent_tools.append(activity)
        return activity
