"""session 层异常类型。

这些异常服务于用户可见的 session catalog、resume 和 share 流程，不暴露底层
JSONL 解析细节给 TUI。
"""

from __future__ import annotations


class SessionError(Exception):
    """session 层通用错误。"""


class SessionNotFoundError(SessionError):
    """请求的 session 不存在。"""


class SessionInvalidIdError(SessionError):
    """session_id 不是安全的单文件名。"""


class SessionEmptyError(SessionError):
    """session 存在但没有可恢复事件。"""


class SessionCorruptError(SessionError):
    """session 事件日志损坏，无法安全 resume 或导出。"""


class SessionUnsupportedSchemaError(SessionError):
    """session 使用当前运行时不支持的 context event schema。"""

    def __init__(self, *, session_id: str, actual_version: str, expected_version: str) -> None:
        self.session_id = session_id
        self.actual_version = actual_version
        self.expected_version = expected_version
        super().__init__(f"session {session_id} uses context event schema {actual_version}; " f"expected {expected_version}")
