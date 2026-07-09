"""Coding-agent adapters used by benchmark runners."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Protocol

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.eval.context_metrics import collect_context_metrics
from firstcoder.eval.patch import collect_git_diff
from firstcoder.eval.tasks import CodingTask, CodingTaskResult
from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionAction, PermissionDecision, PermissionDecisionKind, PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError
from firstcoder.providers.factory import create_provider
from firstcoder.providers.types import ChatRequest, ChatResponse
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.utils.sandbox_access import SandboxAccess


class CodingAgentAdapter(Protocol):
    def run_task(self, task: CodingTask) -> CodingTaskResult:
        ...


LoopFactory = Callable[[CodingTask, Path], AgentLoop]
ProviderFactory = Callable[[str | None], ChatProvider]
_UNSAFE_SESSION_DIR_CHARS = re.compile(r"[/\\:]")


class FirstCoderCodingAgentAdapter:
    """Runs FirstCoder against one repository-level coding task."""

    def __init__(
        self,
        *,
        model_name_or_path: str = "firstcoder",
        provider_name: str | None = None,
        session_root: str | Path = ".firstcoder-eval",
        limits: AgentLoopLimits | None = None,
        provider_retries: int = 3,
        provider_retry_initial_delay_seconds: float = 2.0,
        loop_factory: LoopFactory | None = None,
        provider_factory: ProviderFactory = create_provider,
    ) -> None:
        self.model_name_or_path = model_name_or_path
        self.provider_name = provider_name
        self.session_root = Path(session_root)
        self.limits = limits
        self.provider_retries = provider_retries
        self.provider_retry_initial_delay_seconds = provider_retry_initial_delay_seconds
        self.loop_factory = loop_factory or self._create_loop
        self.provider_factory = provider_factory

    def run_task(self, task: CodingTask) -> CodingTaskResult:
        session_root = self._session_root_for_task(task)
        session_root.mkdir(parents=True, exist_ok=True)
        loop = self.loop_factory(task, session_root)
        response = loop.run_user_turn(_build_task_prompt(task))
        transcript_path = session_root / "sessions" / f"{_session_dir_name(task.instance_id)}.jsonl"
        return CodingTaskResult(
            instance_id=task.instance_id,
            model_name_or_path=self.model_name_or_path,
            model_patch=collect_git_diff(task.repo_path, include_untracked=True),
            transcript_path=transcript_path,
            raw_response=response.content,
            context_metrics=collect_context_metrics(transcript_path),
        )

    def _session_root_for_task(self, task: CodingTask) -> Path:
        root = self.session_root
        if not root.is_absolute():
            root = task.repo_path.resolve().parent / root
        session_root = (root / _session_dir_name(task.instance_id)).resolve()
        repo = task.repo_path.resolve()
        if session_root == repo or repo in session_root.parents:
            raise ValueError("Benchmark session_root must resolve outside the task repository.")
        return session_root

    def _create_loop(self, task: CodingTask, session_root: Path) -> AgentLoop:
        sandbox_access = SandboxAccess()
        registry = create_builtin_registry(
            task.repo_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=False,
            access=sandbox_access,
        )
        permission_manager = PermissionManager(
            policy=BenchmarkPermissionPolicy(task.repo_path),
            grants=PermissionGrantStore(),
            mode=PermissionMode.BYPASS,
        )
        store = JsonlSessionStore(session_root)
        tools = registry.tools()
        session = AgentSession.from_project(
            store=store,
            session_id=_session_dir_name(task.instance_id),
            project_root=task.repo_path,
            tools=tools,
            permission_manager=permission_manager,
            sandbox_access=sandbox_access,
        )
        return AgentLoop(
            session=session,
            provider=self._create_provider(),
            tools=tools,
            limits=self.limits or AgentLoopLimits.swe_lite(),
        )

    def _create_provider(self) -> ChatProvider:
        provider = self.provider_factory(self.provider_name)
        if self.provider_retries <= 0:
            return provider
        return RetryableBenchmarkProvider(
            provider,
            max_retries=self.provider_retries,
            initial_delay_seconds=self.provider_retry_initial_delay_seconds,
        )


class RetryableBenchmarkProvider(ChatProvider):
    """Retry transient provider failures during non-interactive benchmark runs."""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        max_retries: int,
        initial_delay_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self.initial_delay_seconds = max(0.0, initial_delay_seconds)
        self.sleep = sleep

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    def complete(self, request: ChatRequest) -> ChatResponse:
        attempt = 0
        while True:
            try:
                return self.provider.complete(request)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                delay = self.initial_delay_seconds * (2**attempt)
                if delay > 0:
                    self.sleep(delay)
                attempt += 1


class BenchmarkPermissionPolicy(DefaultPermissionPolicy):
    """Non-interactive benchmark policy for repo-local edits."""

    def decide(self, request, *, mode: PermissionMode) -> PermissionDecision:
        if request.action == PermissionAction.EXECUTE_SHELL:
            command = request.target.strip()
            if self._request_cwd_inside_root(request) and (
                command == "python -m pytest"
                or command.startswith("python -m pytest ")
                or command == "python3 -m pytest"
                or command.startswith("python3 -m pytest ")
            ):
                return PermissionDecision(
                    kind=PermissionDecisionKind.ALLOW,
                    reason="Benchmarks allow local pytest validation inside the task repository.",
                )
        if request.action == PermissionAction.WRITE_PATH:
            target = self._resolve_path(request.target, cwd=request.cwd)
            if self._is_inside_project(target) and not self._is_sensitive_path(target):
                return PermissionDecision(
                    kind=PermissionDecisionKind.ALLOW,
                    reason="Benchmarks allow non-sensitive writes inside the task repository.",
                )
        return super().decide(request, mode=mode)


def _build_task_prompt(task: CodingTask) -> str:
    base_commit = task.base_commit or "unknown"
    return (
        "You are running inside a SWE-bench style benchmark task.\n"
        f"Instance: {task.instance_id}\n"
        f"Base commit: {base_commit}\n\n"
        "Problem statement:\n"
        f"{task.problem_statement.strip()}\n\n"
        "Return by editing files in the repository. Do not write a final patch manually. "
        "Use tests when useful, keep changes minimal, and leave the repository with the fix applied."
    )


def _session_dir_name(instance_id: str) -> str:
    safe = _UNSAFE_SESSION_DIR_CHARS.sub("_", instance_id)
    while ".." in safe:
        safe = safe.replace("..", "__")
    while "___" in safe:
        safe = safe.replace("___", "__")
    return safe or "instance"
