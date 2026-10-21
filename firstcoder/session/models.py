"""session 层用户可见数据模型。

这里的模型不替代 `firstcoder.context.models.SessionView`。`SessionView` 仍表示从
event log 重放出的会话事实；本文件中的结构用于 resume 列表、分享选项和只读
transcript 等用户入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from firstcoder.agent.session import AgentSession


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
    status: SessionStatus | str = "ok"          # ok/empty/corrupt: 损坏隔离的标记, corrupt 会话仍可展示但不可 resume
    error: str | None = None                    # status 非 ok 时的诊断信息 (如损坏原因)
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

    include_event_ids: bool = False              # 是否导出事件 id (默认不暴露内部标识)
    include_compaction_metadata: bool = False    # 是否导出压缩过程元数据 (默认隐藏实现细节)
    include_tool_calls: bool = True              # 工具调用参数导出 (展示 agent 做了什么)
    include_tool_results: bool = False           # 工具结果默认不展开, 防止大段输出刷屏
    max_tool_result_chars: int = 1200            # 工具结果导出的字符上限
    redact_paths: bool = True                    # 脱敏本地绝对路径
    redact_secrets: bool = True                  # 脱敏密钥/token 等
    archive_mode: ArchiveMode | str = "placeholder"  # 已归档结果只给占位符; preview_only 才附带截断预览


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


@dataclass(slots=True)
class ResumeResult:
    """ResumeService 的返回值。"""

    session: AgentSession
    record: SessionRecord
