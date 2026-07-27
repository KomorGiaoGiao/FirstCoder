"""Opt-in Harbor job plugin for Aider-style verifier-feedback trials."""

from __future__ import annotations

from typing import Any

from harbor.models.job.plugin import BaseJobPlugin

from benchmark.harbor.aider_feedback_trial import create_aider_feedback_trial


class AiderFeedbackPlugin(BaseJobPlugin):
    """Use :class:`AiderFeedbackTrial` only for the lifetime of one job."""

    def __init__(self) -> None:
        self._original_create: Any | None = None
        self._original_create_descriptor: Any | None = None

    async def on_job_start(self, job: Any) -> None:
        from harbor.trial.trial import Trial

        self._original_create = Trial.create
        self._original_create_descriptor = Trial.__dict__["create"]

        async def create_with_feedback(cls: type[Trial], config: Any) -> Trial:
            feedback_trial = await create_aider_feedback_trial(cls, config)
            if feedback_trial is not None:
                return feedback_trial
            return await self._original_create(config)

        Trial.create = classmethod(create_with_feedback)

    async def on_job_end(self, job_result: Any) -> None:
        if self._original_create_descriptor is None:
            return
        from harbor.trial.trial import Trial

        Trial.create = self._original_create_descriptor
        self._original_create = None
        self._original_create_descriptor = None
