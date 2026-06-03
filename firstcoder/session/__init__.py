"""用户可见 session 能力的边界模块。

`firstcoder.context` 仍然负责底层事件日志、上下文投影、checkpoint 和压缩；
本包后续只承载 catalog、resume 编排、只读 transcript 和 share export 等用户入口。
"""

from firstcoder.session.errors import SessionCorruptError, SessionError, SessionInvalidIdError, SessionNotFoundError
from firstcoder.session.models import (
    RedactionOptions,
    SessionRecord,
    ShareOptions,
    Transcript,
    TranscriptEntry,
)

__all__ = [
    "RedactionOptions",
    "SessionCorruptError",
    "SessionError",
    "SessionInvalidIdError",
    "SessionNotFoundError",
    "SessionRecord",
    "ShareOptions",
    "Transcript",
    "TranscriptEntry",
]
