"""同步与流式 provider 调用共享的瞬态失败退避策略。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderRetryPolicy:
    """限制瞬态错误重试次数，并生成确定性的指数退避。"""

    max_retries: int = 2
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 8.0

    def delay_for_retry(self, retry_number: int) -> float:
        if retry_number < 1:
            raise ValueError("retry_number 必须大于 0")
        delay = self.initial_delay_seconds * (self.multiplier ** (retry_number - 1))
        return min(delay, self.max_delay_seconds)


DEFAULT_PROVIDER_RETRY_POLICY = ProviderRetryPolicy()
