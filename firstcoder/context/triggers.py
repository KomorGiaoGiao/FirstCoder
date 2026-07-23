"""上下文压缩的非窗口配置与动态触发判断。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.context.models import SessionView


@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    """L1-L3 内容策略配置。

    窗口容量、高低水位和输出预留属于每次 provider 请求的 ``ContextBudget``，
    不在这里维护第二套固定阈值。
    """

    l2_result_target_tokens: int = 800
    large_tool_result_tokens: int = 1_200
    max_turn_tool_result_tokens: int = 4_000
    max_tail_messages: int = 120
    cold_turn_distance: int = 8
    cold_preview_chars: int = 160


@dataclass(frozen=True, slots=True)
class ContextTriggerDecision:
    should_compact: bool
    reason: str
    estimated_tokens: int
    target_tokens: int


def evaluate_context_triggers(
    view: SessionView,
    config: ContextCompactionConfig,
    *,
    input_tokens: int,
    high_watermark: int,
    low_watermark: int,
) -> ContextTriggerDecision:
    """使用调用方已经计算好的 provider-facing 预算判断普通 AUTO。

    ``view`` 和 ``config`` 保留在接口中，供调用方用同一个入口附带诊断信息；
    普通 AUTO 的触发事实只有动态高水位，避免大结果或消息数形成旁路。
    """

    del view, config
    if input_tokens >= high_watermark:
        return ContextTriggerDecision(True, "token_threshold", input_tokens, low_watermark)
    return ContextTriggerDecision(False, "under_threshold", input_tokens, low_watermark)
