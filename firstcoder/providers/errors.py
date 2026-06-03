"""provider 错误分类。

agent 主循环后续只应该依赖这些分类决定重试、压缩或提示用户，不应该到处解析各家
provider 的错误字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProviderErrorKind(StrEnum):
    PROMPT_TOO_LONG = "prompt_too_long"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    CONFIG_ERROR = "config_error"
    UNSUPPORTED = "unsupported"
    SERVER_ERROR = "server_error"
    API_ERROR = "api_error"
    USER_ABORT = "user_abort"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ProviderError(Exception):
    kind: ProviderErrorKind
    message: str

    @property
    def retryable(self) -> bool:
        """是否适合按普通瞬时错误策略直接重试。"""

        return self.kind in {
            ProviderErrorKind.TIMEOUT,
            ProviderErrorKind.RATE_LIMIT,
            ProviderErrorKind.NETWORK_ERROR,
            ProviderErrorKind.SERVER_ERROR,
        }

    @property
    def requires_compaction(self) -> bool:
        """是否需要先触发上下文压缩，再尝试恢复。"""

        return self.kind == ProviderErrorKind.PROMPT_TOO_LONG


def classify_provider_exception(exc: BaseException) -> ProviderErrorKind:
    """把 SDK/HTTP 异常归类成 provider 内部错误。

    OpenAI SDK 和兼容厂商 SDK 常见做法是把 HTTP 状态码挂在异常对象或 response
    对象上。这里集中读取这些约定，避免 agent loop 依赖某个 SDK 的异常类型。
    """

    return classify_provider_error(str(exc), status_code=_read_status_code(exc))


def classify_provider_error(message: str, *, status_code: int | None = None) -> ProviderErrorKind:
    text = message.lower()

    if "context length" in text or "prompt too long" in text or "maximum context" in text:
        return ProviderErrorKind.PROMPT_TOO_LONG
    if status_code in {401, 403}:
        return ProviderErrorKind.AUTH_ERROR
    if status_code == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status_code == 408:
        return ProviderErrorKind.TIMEOUT
    if status_code is not None and 500 <= status_code <= 599:
        return ProviderErrorKind.SERVER_ERROR
    if status_code is not None and 400 <= status_code <= 499:
        return ProviderErrorKind.API_ERROR
    if "rate limit" in text or "429" in text:
        return ProviderErrorKind.RATE_LIMIT
    if "api key" in text or "unauthorized" in text or "401" in text or "403" in text:
        return ProviderErrorKind.AUTH_ERROR
    if "timeout" in text or "timed out" in text:
        return ProviderErrorKind.TIMEOUT
    if "network" in text or "connection" in text:
        return ProviderErrorKind.NETWORK_ERROR
    if "abort" in text or "cancel" in text:
        return ProviderErrorKind.USER_ABORT
    if "500" in text or "502" in text or "503" in text or "504" in text or "server error" in text:
        return ProviderErrorKind.SERVER_ERROR
    if "api" in text or "server" in text:
        return ProviderErrorKind.API_ERROR
    return ProviderErrorKind.UNKNOWN


def _read_status_code(exc: BaseException) -> int | None:
    for name in ("status_code", "status", "http_status"):
        status = _coerce_status_code(getattr(exc, name, None))
        if status is not None:
            return status

    response = getattr(exc, "response", None)
    if response is not None:
        status = _coerce_status_code(getattr(response, "status_code", None))
        if status is not None:
            return status
    return None


def _coerce_status_code(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
