from __future__ import annotations

from dataclasses import dataclass, field
import re

import pytest

from benchmark.task_boundary_compaction.loop import BenchmarkAgentLoop
from benchmark.task_boundary_compaction.models import Arm
from firstcoder.agent.session import AgentSession
from firstcoder.context.manager import ContextCompactResult, ContextWindowTrigger
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


@dataclass
class ScriptedBoundaryProvider(ChatProvider):
    decisions: list[str]
    requests: list[ChatRequest] = field(default_factory=list)
    classification_requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            self.classification_requests.append(request)
            return ChatResponse(
                provider=self.name,
                model=self.model,
                content=(
                    '{"decision":"'
                    + self.decisions.pop(0)
                    + '","basis_message_id":"'
                    + _basis_message_id(request)
                    + '"}'
                ),
            )
        self.requests.append(request)
        return ChatResponse(provider=self.name, model=self.model, content="完成")


@dataclass
class RecordingContextManager:
    calls: list[object] = field(default_factory=list)

    def compact_if_needed(self, request):
        self.calls.append(request)
        return ContextCompactResult(
            status="skipped",
            reason="under_threshold",
            view=request.view,
            before_tokens=request.budget.input_tokens,
            after_tokens=request.budget.input_tokens,
        )


def _basis_message_id(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        match = re.search(r"basis_message_id=([A-Za-z0-9_]+)", message.content)
        if match:
            return match.group(1)
    raise AssertionError("classifier request did not expose its basis message id")


def _run_two_turn_boundary_case(tmp_path, arm: Arm, decisions: list[str]):
    store = JsonlSessionStore(tmp_path / arm.value)
    session = AgentSession.create(store=store, session_id=f"session_{arm.value}", agents_md="")
    session.runtime_state.active_task_hash = "task_a"
    provider = ScriptedBoundaryProvider(decisions=decisions)
    context_manager = RecordingContextManager()
    loop = BenchmarkAgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
        arm=arm,
    )

    loop._run_user_turn_sync("任务 B：修复 parser")
    loop._run_user_turn_sync("继续任务 B：运行测试")

    return store, session, provider, context_manager


@pytest.mark.parametrize(
    ("arm", "expected_classifier_calls", "expected_task_hash_changed_calls"),
    [
        (Arm.AUTO_ONLY, 0, 0),
        (Arm.CLASSIFIER_ONLY, 2, 0),
        (Arm.FULL, 2, 1),
    ],
)
def test_three_arms_keep_auto_but_isolate_task_boundary_compaction(
    tmp_path,
    arm: Arm,
    expected_classifier_calls: int,
    expected_task_hash_changed_calls: int,
) -> None:
    store, session, provider, context_manager = _run_two_turn_boundary_case(
        tmp_path,
        arm,
        decisions=["new", "same"],
    )

    triggers = [call.trigger for call in context_manager.calls]
    event_types = [event.type for event in store.list_events(session.session_id)]

    assert len(provider.requests) == 2
    assert len(provider.classification_requests) == expected_classifier_calls
    assert triggers.count(ContextWindowTrigger.AUTO) == 2
    assert triggers.count(ContextWindowTrigger.TASK_HASH_CHANGED) == expected_task_hash_changed_calls
    assert event_types.count("task_boundary_observed") == (2 if arm is not Arm.AUTO_ONLY else 0)


def test_full_arm_does_not_compact_a_same_task_negative_case(tmp_path) -> None:
    store, session, provider, context_manager = _run_two_turn_boundary_case(
        tmp_path,
        Arm.FULL,
        decisions=["same", "same"],
    )

    triggers = [call.trigger for call in context_manager.calls]
    event_types = [event.type for event in store.list_events(session.session_id)]

    assert len(provider.classification_requests) == 2
    assert triggers.count(ContextWindowTrigger.AUTO) == 2
    assert ContextWindowTrigger.TASK_HASH_CHANGED not in triggers
    assert event_types.count("task_boundary_observed") == 2
