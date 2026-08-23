"""Benchmark-only AgentLoop variants for the three causal experiment arms."""

from __future__ import annotations

from firstcoder.agent.loop import AgentLoop
from firstcoder.context.manager import ContextWindowTrigger

from benchmark.task_boundary_compaction.models import Arm


class BenchmarkAgentLoop(AgentLoop):
    """Preserve production control flow while selectively disabling one arm behavior."""

    def __init__(self, *args, arm: Arm, **kwargs) -> None:
        self.arm = arm
        super().__init__(*args, **kwargs)

    def _classify_task_boundary(self, basis_message_id: str) -> None:
        if self.arm is not Arm.AUTO_ONLY:
            super()._classify_task_boundary(basis_message_id)

    async def _classify_task_boundary_async(self, basis_message_id: str) -> None:
        if self.arm is not Arm.AUTO_ONLY:
            await super()._classify_task_boundary_async(basis_message_id)

    def _compact_if_needed(
        self,
        *,
        trigger: ContextWindowTrigger,
        runtime_instruction: str | None = None,
    ):
        if self.arm is Arm.CLASSIFIER_ONLY and trigger is ContextWindowTrigger.TASK_HASH_CHANGED:
            return None
        return super()._compact_if_needed(
            trigger=trigger,
            runtime_instruction=runtime_instruction,
        )
