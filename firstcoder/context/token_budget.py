"""上下文 token 预算的集中估算。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.context.models import AgentMessage, MessagePart


def estimate_text_tokens(text: str) -> int:
    """第一版使用字符数近似 token。

    这里有意不绑定具体 tokenizer，避免 context 层过早依赖 provider。后续可以按 provider
    能力替换实现，但调用点仍然走 `TokenBudgetService`。
    """

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class TokenBudget:
    context_window: int
    reserved_output_tokens: int
    effective_window: int
    warning_threshold: int
    auto_compact_threshold: int
    blocking_threshold: int


@dataclass(slots=True)
class TokenBudgetService:
    context_window: int
    provider_max_output_tokens: int

    def build_budget(self) -> TokenBudget:
        reserved = min(self.provider_max_output_tokens, 16_000)
        effective = max(0, self.context_window - reserved)
        return TokenBudget(
            context_window=self.context_window,
            reserved_output_tokens=reserved,
            effective_window=effective,
            warning_threshold=effective * 70 // 100,
            auto_compact_threshold=effective * 82 // 100,
            blocking_threshold=effective * 95 // 100,
        )

    def estimate_part_tokens(self, part: MessagePart) -> int:
        return estimate_text_tokens(part.content)

    def estimate_message_tokens(self, message: AgentMessage) -> int:
        return sum(self.estimate_part_tokens(part) for part in message.parts)
