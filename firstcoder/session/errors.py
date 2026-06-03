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


class SessionCorruptError(SessionError):
    """session 事件日志损坏，无法安全 resume 或导出。"""
