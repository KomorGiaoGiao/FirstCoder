from firstcoder.context.models import AgentMessage, MessagePart
from firstcoder.context.token_budget import TokenBudgetService


def test_token_budget_service_computes_thresholds_from_context_window() -> None:
    service = TokenBudgetService(context_window=100_000, provider_max_output_tokens=20_000)

    budget = service.build_budget()

    assert budget.reserved_output_tokens == 16_000
    assert budget.effective_window == 84_000
    assert budget.warning_threshold == 58_800
    assert budget.auto_compact_threshold == 68_880
    assert budget.blocking_threshold == 79_800


def test_token_budget_service_estimates_messages() -> None:
    service = TokenBudgetService(context_window=10_000, provider_max_output_tokens=1_000)
    message = AgentMessage(
        id="msg_1",
        session_id="sess_1",
        role="user",
        parts=[
            MessagePart(id="part_1", message_id="msg_1", kind="text", content="abcd" * 100)
        ],
    )

    assert service.estimate_message_tokens(message) == 100
