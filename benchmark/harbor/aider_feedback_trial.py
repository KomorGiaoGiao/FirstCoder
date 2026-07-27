"""Aider-style test-feedback retry for single-step Harbor coding tasks.

This runner is deliberately benchmark-local.  It gives an agent one repair
turn only after the task's real shared verifier produced ``reward=0``.  It does
not expose verifier files during the initial agent turn and never retries
infrastructure failures that did not produce a reward.
"""

from __future__ import annotations

from typing import Any, override

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.task.verifier_mode import VerifierEnvironmentMode, resolve_task_verifier_mode
from harbor.trial.errors import AgentTimeoutError
from harbor.trial.single_step import SingleStepTrial
from harbor.verifier.verifier import RewardFileNotFoundError


_MAX_FEEDBACK_CHARS = 60_000


def should_request_feedback_round(rewards: dict[str, float | int] | None) -> bool:
    """Return true only for a verifier that explicitly reported reward zero."""

    return rewards is not None and rewards.get("reward") == 0


def should_request_feedback_after_missing_reward(test_output: str) -> bool:
    """Recognize the C++ verifier's normal compile-failure path.

    The local C++ verifier exits before writing ``reward.txt`` when CMake
    cannot compile a submission.  Its complete compiler diagnostics are still
    valid Aider-style feedback.  Installation, Docker and timeout failures do
    not include this marker and remain trial errors.
    """

    return "CMake build failed" in test_output and "error:" in test_output.lower()


def build_aider_feedback(test_output: str) -> str:
    """Format the repair turn without implying the agent may change tests."""

    output = test_output[-_MAX_FEEDBACK_CHARS:]
    return (
        "The tests are correct. Do not modify the tests.\n"
        "Fix the code in the current workspace to resolve the testing errors below, "
        "then run the relevant tests if possible.\n\n"
        "Testing errors:\n"
        f"{output}"
    )


class AiderFeedbackTrial(SingleStepTrial):
    """Run one extra same-session repair turn after a failed shared verifier."""

    @override
    async def _run(self) -> None:
        # A separate verifier tears down the agent environment before testing,
        # so it cannot support an in-place repair turn.  Preserve Harbor's
        # standard behavior for such tasks rather than silently changing it.
        if resolve_task_verifier_mode(self.task.config) != VerifierEnvironmentMode.SHARED:
            await super()._run()
            return

        await self._run_agent()
        await self._upload_agent_logs()
        await self._collect_artifacts()
        missing_reward_compile_failure = False
        try:
            await self._run_verifier()
        except RewardFileNotFoundError:
            missing_reward_compile_failure = should_request_feedback_after_missing_reward(
                self._read_verifier_output()
            )
            if not missing_reward_compile_failure:
                raise

        failed_reward = should_request_feedback_round(
            self.result.verifier_result.rewards if self.result.verifier_result else None
        )
        if self.result.exception_info is None and (failed_reward or missing_reward_compile_failure):
            feedback = build_aider_feedback(self._read_verifier_output())
            await self._run_agent_with_instruction(feedback)
            await self._upload_agent_logs()
            await self._run_verifier()

        await self._stop_agent_environment()

    async def _run_agent_with_instruction(self, instruction: str) -> None:
        """Reuse the task environment/session for the feedback message."""

        try:
            await self._run_agent_phase(
                target=self.result,
                instruction=instruction,
                timeout_sec=self._agent_timeout_sec,
                user=self.task.config.agent.user,
            )
        except (AgentTimeoutError, NonZeroAgentExitCodeError) as exc:
            self._record_exception(exc)
        finally:
            await self._sync_agent_output(self.result)

    def _read_verifier_output(self) -> str:
        output = self.paths.verifier_dir / "test-stdout.txt"
        try:
            return output.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "The verifier reported reward=0 but did not provide test output."


async def create_aider_feedback_trial(
    trial_class: Any,
    config: Any,
) -> AiderFeedbackTrial | None:
    """Create the feedback runner for a single-step task, otherwise return None."""

    task, download_result = await trial_class._load_task(config)
    if task.has_steps:
        return None
    return AiderFeedbackTrial(
        config,
        _task=task,
        _task_download_result=download_result,
    )
