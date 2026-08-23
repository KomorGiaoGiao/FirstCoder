from __future__ import annotations

from dataclasses import dataclass

import pytest

from benchmark.task_boundary_compaction.seed import seed_old_task_context
from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, MainRequestOptions


@dataclass
class NoopProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("seeding must not call the provider")


def test_seed_old_task_context_stays_below_auto_high_water_and_marks_old_hash(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="seeded", agents_md="")
    session.runtime_state.active_task_hash = "task_a"
    loop = AgentLoop(
        session=session,
        provider=NoopProvider(),
        context_window=32_768,
        request_options=MainRequestOptions(max_tokens=4_096),
    )

    seeded = seed_old_task_context(
        session,
        case_id="controlled-parser",
        target_input_tokens=21_000,
        estimate_budget=loop.context_budget_for_view,
    )

    view = session.rebuild_view()
    budget = loop.context_budget_for_view(view)
    event_types = [event.type for event in store.list_events(session.session_id)]

    assert seeded.task_hash == "task_a"
    assert seeded.input_tokens == budget.input_tokens
    assert budget.high_watermark * 0.75 <= budget.input_tokens < budget.high_watermark
    assert all(part.metadata["task_hash"] == "task_a" for message in view.messages for part in message.parts)
    assert "checkpoint_created" not in event_types
    assert "compaction_completed" not in event_types
    assert "provider_projection_consumed" not in event_types


def test_seed_rejects_a_target_that_cannot_remain_below_auto_high_water(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="impossible", agents_md="")
    session.runtime_state.active_task_hash = "task_a"
    loop = AgentLoop(
        session=session,
        provider=NoopProvider(),
        context_window=32_768,
        request_options=MainRequestOptions(max_tokens=4_096),
    )
    high_watermark = loop.context_budget_for_view(session.rebuild_view()).high_watermark

    with pytest.raises(ValueError, match="high_watermark"):
        seed_old_task_context(
            session,
            case_id="controlled-parser",
            target_input_tokens=high_watermark,
            estimate_budget=loop.context_budget_for_view,
        )
