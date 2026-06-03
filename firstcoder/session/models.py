"""session 层用户可见数据模型。

这里的模型不替代 `firstcoder.context.models.SessionView`。`SessionView` 仍表示从
event log 重放出的会话事实；本文件中的结构用于 resume 列表、分享选项和只读
transcript 等用户入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SessionStatus = Literal["ok", "empty", "corrupt"]
ArchiveMode = Literal["placeholder", "preview_only"]


@dataclass(slots=True)
class SessionRecord:
    """resume 列表中展示的一条 session 摘要。"""

    session_id: str
    title: str
    created_at: str | None = None
    updated_at: str | None = None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    message_count: int = 0
    user_turn_count: int = 0
    checkpoint_count: int = 0
    archive_count: int = 0
    latest_user_input: str | None = None
    latest_assistant_output: str | None = None
    latest_checkpoint_id: str | None = None
    status: SessionStatus | str = "ok"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RedactionOptions:
    """分享和预览文本的默认脱敏选项。"""

    redact_paths: bool = True
    redact_secrets: bool = True


@dataclass(slots=True)
class ShareOptions:
    """只读 transcript/share 的导出选项。

    默认值保持保守：不展开工具结果，不读取 archive 原文，并脱敏路径和 secret。
    """

    include_event_ids: bool = False
    include_compaction_metadata: bool = False
    include_tool_calls: bool = True
    include_tool_results: bool = False
    max_tool_result_chars: int = 1200
    redact_paths: bool = True
    redact_secrets: bool = True
    archive_mode: ArchiveMode | str = "placeholder"


@dataclass(slots=True)
class TranscriptEntry:
    """只读 transcript 中的一条展示记录。"""

    role: str
    title: str
    content: str
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Transcript:
    """从 event log 派生出的只读 transcript。"""

    session: SessionRecord
    entries: list[TranscriptEntry] = field(default_factory=list)
