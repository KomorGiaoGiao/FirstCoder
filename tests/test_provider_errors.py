from firstcoder.providers.errors import ProviderError, ProviderErrorKind, classify_provider_error


def test_classify_provider_error_from_known_messages() -> None:
    assert classify_provider_error("maximum context length exceeded") == ProviderErrorKind.PROMPT_TOO_LONG
    assert classify_provider_error("429 rate limit exceeded") == ProviderErrorKind.RATE_LIMIT
    assert classify_provider_error("401 invalid api key") == ProviderErrorKind.AUTH_ERROR
    assert classify_provider_error("request timed out") == ProviderErrorKind.TIMEOUT


def test_provider_error_exposes_retry_policy_flags() -> None:
    assert ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long").retryable is True
    assert ProviderError(ProviderErrorKind.AUTH_ERROR, "bad key").retryable is False
    assert ProviderError(ProviderErrorKind.CONFIG_ERROR, "bad config").retryable is False
    assert ProviderError(ProviderErrorKind.USER_ABORT, "cancelled").retryable is False
