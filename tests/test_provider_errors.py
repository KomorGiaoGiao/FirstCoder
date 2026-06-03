from firstcoder.providers.errors import (
    ProviderError,
    ProviderErrorKind,
    classify_provider_error,
    classify_provider_exception,
)


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _ResponseStatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = _Response(status_code)


def test_classify_provider_error_from_known_messages() -> None:
    assert classify_provider_error("maximum context length exceeded") == ProviderErrorKind.PROMPT_TOO_LONG
    assert classify_provider_error("429 rate limit exceeded") == ProviderErrorKind.RATE_LIMIT
    assert classify_provider_error("401 invalid api key") == ProviderErrorKind.AUTH_ERROR
    assert classify_provider_error("request timed out") == ProviderErrorKind.TIMEOUT
    assert classify_provider_error("upstream server error 503") == ProviderErrorKind.SERVER_ERROR


def test_classify_provider_error_from_http_status() -> None:
    assert classify_provider_error("bad key", status_code=401) == ProviderErrorKind.AUTH_ERROR
    assert classify_provider_error("slow down", status_code=429) == ProviderErrorKind.RATE_LIMIT
    assert classify_provider_error("gateway timeout", status_code=504) == ProviderErrorKind.SERVER_ERROR
    assert classify_provider_error("bad request", status_code=400) == ProviderErrorKind.API_ERROR


def test_classify_provider_exception_reads_status_like_attributes() -> None:
    assert classify_provider_exception(_StatusError("temporary outage", 502)) == ProviderErrorKind.SERVER_ERROR
    assert classify_provider_exception(_ResponseStatusError("too many requests", 429)) == ProviderErrorKind.RATE_LIMIT


def test_provider_error_exposes_retry_policy_flags() -> None:
    assert ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long").retryable is False
    assert ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long").requires_compaction is True
    assert ProviderError(ProviderErrorKind.SERVER_ERROR, "server down").retryable is True
    assert ProviderError(ProviderErrorKind.API_ERROR, "bad request").retryable is False
    assert ProviderError(ProviderErrorKind.UNKNOWN, "unknown").retryable is False
    assert ProviderError(ProviderErrorKind.AUTH_ERROR, "bad key").retryable is False
    assert ProviderError(ProviderErrorKind.CONFIG_ERROR, "bad config").retryable is False
    assert ProviderError(ProviderErrorKind.UNSUPPORTED, "not supported").retryable is False
    assert ProviderError(ProviderErrorKind.USER_ABORT, "cancelled").retryable is False
