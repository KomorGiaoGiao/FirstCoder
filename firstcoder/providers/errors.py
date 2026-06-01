"""provider 错误分类。

agent 主循环后续只应该依赖这些分类决定重试、压缩或提示用户，不应该到处解析各家
provider 的错误字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderErrorKind(StrEnum):
    PROMPT_TOO_LONG = "prompt_too_long"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
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
        return self.kind in {
            ProviderErrorKind.PROMPT_TOO_LONG,
            ProviderErrorKind.TIMEOUT,
            ProviderErrorKind.RATE_LIMIT,
            ProviderErrorKind.NETWORK_ERROR,
            ProviderErrorKind.API_ERROR,
            ProviderErrorKind.UNKNOWN,
        }


def classify_provider_error(message: str) -> ProviderErrorKind:
    text = message.lower()

    if "context length" in text or "prompt too long" in text or "maximum context" in text:
        return ProviderErrorKind.PROMPT_TOO_LONG
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
    if "api" in text or "server" in text:
        return ProviderErrorKind.API_ERROR
    return ProviderErrorKind.UNKNOWN
