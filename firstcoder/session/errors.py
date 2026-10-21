"""session 层异常类型。

这些异常服务于用户可见的 session catalog、resume 和 share 流程，不暴露底层
JSONL 解析细节给 TUI。
"""

from __future__ import annotations


class SessionError(Exception):
    """session 层通用错误 (resume/catalog/share 流程的基类)。"""


class SessionNotFoundError(SessionError):
    """请求的 session 不存在: sessions/<id>.jsonl 缺失时由 catalog/resume 抛出。"""


class SessionInvalidIdError(SessionError):
    """session_id 不是安全的单文件名: 含路径分隔符等, 有路径穿越风险时抛出。"""


class SessionEmptyError(SessionError):
    """session 存在但没有任何可恢复事件: 空日志文件 resume 时抛出。"""


class SessionCorruptError(SessionError):
    """session 事件日志损坏 (非法 JSON / 缺 session_created / 重放断链), 无法安全 resume 或导出。"""


class SessionUnsupportedSchemaError(SessionError):
    """session 使用当前运行时不支持的 context event schema: 旧版本/未来版本日志拒绝重放。"""

    def __init__(self, *, session_id: str, actual_version: str, expected_version: str) -> None:
        self.session_id = session_id
        self.actual_version = actual_version
        self.expected_version = expected_version
        super().__init__(f"session {session_id} uses context event schema {actual_version}; " f"expected {expected_version}")
