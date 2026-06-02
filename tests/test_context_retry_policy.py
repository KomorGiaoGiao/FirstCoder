from firstcoder.context.retry_policy import (
    CompactRetryDecision,
    CompactRetryPolicy,
)


def test_prompt_too_long_retries_after_stronger_compaction() -> None:
    policy = CompactRetryPolicy(max_prompt_too_long_retries=1)

    first = policy.decide("prompt_too_long", attempt=1)
    second = policy.decide("prompt_too_long", attempt=2)

    assert first == CompactRetryDecision(
        should_retry=True,
        action="stronger_compaction",
        reason="prompt_too_long",
    )
    assert second.should_retry is False


def test_timeout_uses_limited_backoff_retries() -> None:
    policy = CompactRetryPolicy(max_timeout_retries=2)

    assert policy.decide("timeout", attempt=1).action == "backoff"
    assert policy.decide("timeout", attempt=2).should_retry is True
    assert policy.decide("timeout", attempt=3).should_retry is False


def test_no_summary_has_bounded_retry() -> None:
    policy = CompactRetryPolicy(max_no_summary_retries=1)

    assert policy.decide("no_summary", attempt=1).should_retry is True
    assert policy.decide("no_summary", attempt=2).should_retry is False


def test_unknown_error_does_not_retry() -> None:
    policy = CompactRetryPolicy()

    decision = policy.decide("provider_error", attempt=1)

    assert decision.should_retry is False
    assert decision.action == "fail"
