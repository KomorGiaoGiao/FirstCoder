import pytest

from firstcoder.context.token_budget import build_context_budget
from firstcoder.context.budget_defaults import DEFAULT_CONTEXT_WINDOW
from firstcoder.providers.types import ChatMessage, ContentPart, ToolDefinition


@pytest.mark.parametrize(
    ("window", "output", "capacity", "high", "low"),
    [
        (32_768, 4_096, 27_033, 24_329, 19_463),
        (128_000, 8_192, 113_408, 102_067, 81_653),
        (200_000, 8_192, 181_808, 163_627, 130_901),
    ],
)
def test_budget_uses_dynamic_watermarks(window, output, capacity, high, low) -> None:
    budget = build_context_budget(
        messages=[],
        tools=[],
        context_window=window,
        max_output_tokens=output,
    )

    assert budget.input_capacity == capacity
    assert budget.high_watermark == high
    assert budget.low_watermark == low


def test_budget_separates_fixed_and_history_tokens() -> None:
    budget = build_context_budget(
        messages=[
            ChatMessage(role="system", content="s" * 40),
            ChatMessage(role="user", content="u" * 80),
        ],
        tools=[ToolDefinition(name="read", description="d" * 40, parameters={})],
        context_window=32_768,
        max_output_tokens=4_096,
    )

    assert budget.fixed_tokens > 10
    assert budget.history_tokens == 20
    assert budget.input_tokens == budget.fixed_tokens + budget.history_tokens


def test_budget_counts_image_once_without_counting_base64_bytes() -> None:
    budget = build_context_budget(
        messages=[
            ChatMessage(
                role="user",
                content="describe",
                content_parts=[
                    ContentPart(type="text", text="describe"),
                    ContentPart(type="image", media_type="image/png", data_base64="x" * 100_000),
                ],
            )
        ],
        tools=[],
        context_window=None,
        max_output_tokens=None,
    )

    assert budget.source == "assumed"
    assert budget.context_window == DEFAULT_CONTEXT_WINDOW == 200_000
    assert budget.history_tokens < 2_000


@pytest.mark.parametrize(
    ("window", "output"),
    [(0, 1), (1_000, 1_000), (1_000, 2_000)],
)
def test_budget_rejects_invalid_capacity(window, output) -> None:
    with pytest.raises(ValueError):
        build_context_budget(
            messages=[],
            tools=[],
            context_window=window,
            max_output_tokens=output,
        )
