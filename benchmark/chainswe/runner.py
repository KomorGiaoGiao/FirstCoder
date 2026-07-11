"""Sequential, persistent-session runner for ChainSWE chains.

The runner intentionally has no Docker or dataset-download dependency.  A
container launcher can prepare a ChainSWE checkout, then call :func:`run_chain`
with that checkout and a persistent agent.  This module owns the important
benchmark contract inside that checkout: one workspace and one agent session
are reused for every ordered issue in a chain.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark.chainswe.models import ChainSWEChain, ChainSWEIssue, load_chains_jsonl, parse_chain_record, select_chain
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.app.factory import create_firstcoder_app
from firstcoder.config import load_config
from firstcoder.eval.context_metrics import collect_context_metrics
from firstcoder.eval.patch import collect_git_diff
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import create_provider_from_config
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.tools.session_registry import create_session_tool_registry
from firstcoder.utils.sandbox_access import SandboxAccess, SandboxAccessMode


CHAIN_TASK_INSTRUCTIONS = """You are solving one issue in a continuous ChainSWE benchmark.
Work only from the problem statement below and modify the current repository to
implement the requested fix. Keep prior fixes intact. Do not ask for, search
for, or rely on benchmark test patches, test names, or hidden verifier commands.
You may inspect the repository and run appropriate local validation. Do not
modify test files, test fixtures, benchmark files, or verifier configuration:
hidden ChainSWE test patches must remain applicable. Leave the working tree
containing only your implementation changes when you finish.

Problem statement:
"""


@dataclass(frozen=True, slots=True)
class ChainAgentResponse:
    """The agent-visible output of one persistent-session benchmark turn."""

    response: str = ""
    context_metrics: dict[str, Any] = field(default_factory=dict)


class ChainAgent(Protocol):
    """Minimal agent boundary used by the generic runner and its fake tests.

    The protocol deliberately accepts a prompt string rather than a
    ``ChainSWEIssue``.  It makes accidental leakage of ``test_patch`` or test
    commands to an agent implementation structurally harder.
    """

    def run_task(self, prompt: str) -> ChainAgentResponse:
        """Run exactly one task in the agent's existing session."""


@dataclass(frozen=True, slots=True)
class VerifierResult:
    """Outcome from applying a verifier patch and running its command."""

    passed: bool
    exit_code: int | None
    output: str
    test_cmds: str | None
    test_patch_applied: bool
    test_patch_exit_code: int | None = None
    test_patch_output: str = ""
    exception: str | None = None
    replayed_test_patches: tuple["VerifierPatchResult", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["replayed_test_patches"] = [patch.to_dict() for patch in self.replayed_test_patches]
        return data


@dataclass(frozen=True, slots=True)
class VerifierPatchResult:
    """One hidden test patch replayed in a disposable verifier worktree."""

    order: int
    swebench_instance_id: str
    applied: bool
    exit_code: int | None
    output: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChainIssueResult:
    """All durable artifacts produced for one issue in a chain."""

    task: ChainSWEIssue
    model_patch: str
    response: str
    agent_exception: str | None
    elapsed_seconds: float
    git_commits: dict[str, str | None]
    verifier: VerifierResult
    context_metrics: dict[str, Any]
    runner_exception: str | None = None

    @property
    def passed(self) -> bool:
        return self.verifier.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": _issue_to_dict(self.task),
            "model_patch": self.model_patch,
            "response": self.response,
            "agent_exception": self.agent_exception,
            "runner_exception": self.runner_exception,
            "elapsed_seconds": self.elapsed_seconds,
            "git_commits": dict(self.git_commits),
            "verifier": self.verifier.to_dict(),
            "context_metrics": dict(self.context_metrics),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ChainRunResult:
    """Serializable score and per-issue trace for one ChainSWE chain."""

    chain: ChainSWEChain
    workspace: Path
    results: tuple[ChainIssueResult, ...]
    reset_workspace: bool

    @property
    def passed_bug_count(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def per_bug_success(self) -> float:
        return self.passed_bug_count / len(self.results) if self.results else 0.0

    @property
    def per_bug_success_count(self) -> int:
        """Number of issues whose verifier command passed."""

        return self.passed_bug_count

    @property
    def full_chain_success(self) -> bool:
        return bool(self.results) and all(result.passed for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": _chain_to_dict(self.chain),
            "workspace": str(self.workspace),
            "reset_workspace": self.reset_workspace,
            "results": [result.to_dict() for result in self.results],
            "passed_bug_count": self.passed_bug_count,
            "total_bug_count": len(self.results),
            "per_bug_success": self.per_bug_success,
            "per_bug_success_count": self.per_bug_success_count,
            "full_chain_success": self.full_chain_success,
        }


class FirstCoderChainAgent:
    """Production ChainSWE agent with one app and one persistent session.

    ``create_firstcoder_app`` is called once per chain.  Every ``run_task``
    therefore appends to the same FirstCoder session, allowing the benchmark to
    measure actual sequential context behavior instead of isolated rollouts.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        data_root: str | Path,
        provider: ChatProvider | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        session_id: str = "chainswe",
        max_tool_rounds: int | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.data_root = Path(data_root).resolve()
        _require_data_root_outside_workspace(self.workspace, self.data_root)

        resolved_provider = provider or _create_provider(
            provider_name=provider_name,
            model=model,
            project_root=self.workspace,
        )
        # ``create_firstcoder_app`` receives the prepared tool instances.  Use
        # the same unrestricted benchmark posture as the session's later
        # ``bypass`` mode; tools otherwise retain an independent project-only
        # access object captured at construction time.
        access = SandboxAccess(mode=SandboxAccessMode.UNRESTRICTED)
        tools = create_builtin_registry(
            self.workspace,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=False,
            access=access,
        ).tools()
        self.app = create_firstcoder_app(
            project_root=self.workspace,
            data_root=self.data_root,
            provider=resolved_provider,
            session_id=session_id,
            tools=tools,
        )
        self.app.current_session.set_permission_mode(PermissionMode.BYPASS)
        self._rebuild_chain_session_tool_registry()
        limits = AgentLoopLimits.swe_lite()
        if max_tool_rounds is not None:
            limits = limits.with_max_tool_rounds(max_tool_rounds)
        self.app.chat_runner.limits = limits
        self.session_id = self.app.current_session.session.session_id

    @property
    def provider(self) -> ChatProvider:
        return self.app.chat_runner.provider

    def run_task(self, prompt: str) -> ChainAgentResponse:
        response = self.app.chat_runner.run_user_turn(prompt)
        transcript = self.data_root / "sessions" / f"{self.session_id}.jsonl"
        return ChainAgentResponse(
            response=response.content,
            context_metrics=collect_context_metrics(transcript),
        )

    def _rebuild_chain_session_tool_registry(self) -> None:
        """Use one-observation task boundaries for benchmark task switches.

        App construction injects session-scoped tools before benchmark bypass is
        selected, using the interactive default of two stable observations.
        ChainSWE has externally ordered, unambiguous task changes, so rebuild
        only that session registry after bypass with the benchmark-specific
        threshold.  The existing non-session tools, permissions and runtime
        state remain the same objects.
        """

        session = self.app.current_session.session
        base_tools = [
            tool
            for tool in session.tool_registry.tools()
            if tool.name not in {"task_boundary", "retrieve_archive"}
        ]
        session.tool_registry = create_session_tool_registry(
            session_id=session.session_id,
            runtime_state=session.runtime_state,
            tools=base_tools,
            known_message_ids=session.known_message_ids,
            task_boundary_required_stable_count=1,
            permission_manager=session.permission_manager,
            archive_root=session.store.root,
            current_turn=lambda: session.writer.current_turn,
        )


def run_chain(
    chain: ChainSWEChain,
    *,
    workspace: str | Path,
    agent: ChainAgent,
    reset_workspace: bool = True,
    progress_out: str | Path | None = None,
) -> ChainRunResult:
    """Run every issue in order without replacing the workspace or agent.

    The model's uncommitted work is committed before verification.  Each
    verifier runs in a disposable detached worktree created from that model
    commit, where every chain test patch up through the current issue is
    replayed.  Hidden tests therefore never enter the persistent agent
    workspace. Agent and verifier failures are recorded per issue and never
    prevent later issues from running.
    """

    repo = Path(workspace).resolve()
    if reset_workspace:
        reset_chain_workspace(repo, chain)

    results: list[ChainIssueResult] = []
    verifier_issues: list[ChainSWEIssue] = []
    progress_path = Path(progress_out).resolve() if progress_out is not None else None
    for issue in chain.issues:
        verifier_issues.append(issue)
        result = _run_issue(
            issue,
            repo=repo,
            agent=agent,
            verifier_issues=tuple(verifier_issues),
        )
        results.append(result)
        if progress_path is not None:
            _write_progress_snapshot(
                progress_path,
                chain=chain,
                workspace=repo,
                reset_workspace=reset_workspace,
                results=results,
            )
    return ChainRunResult(
        chain=chain,
        workspace=repo,
        results=tuple(results),
        reset_workspace=reset_workspace,
    )


def _write_progress_snapshot(
    output: Path,
    *,
    chain: ChainSWEChain,
    workspace: Path,
    reset_workspace: bool,
    results: list[ChainIssueResult],
) -> None:
    """Atomically expose safe per-issue progress without leaking hidden tests.

    ``output`` is mounted where the benchmark agent can technically inspect it.
    During a chain, never write hidden patch text, test commands, or verifier
    output there: later issues must not gain information from earlier hidden
    verification. The final summary is written only after the agent has
    completed all issues.
    """

    completed = tuple(results)
    payload = {
        "run_status": "running",
        "chain": {
            "continuous_id": chain.continuous_id,
            "repo": chain.repo,
        },
        "workspace": str(workspace),
        "reset_workspace": reset_workspace,
        "completed_issue_count": len(completed),
        "total_issue_count": len(chain.issues),
        "passed_issue_count": sum(result.passed for result in completed),
        "results": [_progress_result_to_dict(result) for result in completed],
    }
    _write_json_atomic(output, payload)


def _progress_result_to_dict(result: ChainIssueResult) -> dict[str, Any]:
    """Serialize just enough state for live monitoring, never verifier inputs."""

    verifier = result.verifier
    return {
        "order": result.task.order,
        "swebench_instance_id": result.task.swebench_instance_id,
        "passed": result.passed,
        "elapsed_seconds": result.elapsed_seconds,
        "agent_status": "failed" if result.agent_exception else "completed",
        "runner_status": "failed" if result.runner_exception else "completed",
        "verification": {
            "status": "passed" if verifier.passed else "failed",
            "exit_code": verifier.exit_code,
        },
        "context_metrics": dict(result.context_metrics),
    }


def _write_json_atomic(output: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON artifact atomically so observers never read partial data."""

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(output)


def reset_chain_workspace(workspace: str | Path, chain: ChainSWEChain) -> None:
    """Restore a checkout to the chain base and remove all generated files."""

    repo = Path(workspace).resolve()
    _run_checked(["git", "reset", "--hard", chain.base_commit], cwd=repo)
    _run_checked(["git", "clean", "-fdx"], cwd=repo)


def build_task_prompt(problem_statement: str) -> str:
    """Create the only ChainSWE task payload made visible to the agent."""

    return f"{CHAIN_TASK_INSTRUCTIONS}{problem_statement.strip()}\n"


def _run_issue(
    issue: ChainSWEIssue,
    *,
    repo: Path,
    agent: ChainAgent,
    verifier_issues: tuple[ChainSWEIssue, ...],
) -> ChainIssueResult:
    started = time.monotonic()
    response = ""
    context_metrics: dict[str, Any] = {}
    agent_exception: str | None = None
    runner_exception: str | None = None

    try:
        agent_result = agent.run_task(build_task_prompt(issue.problem_statement))
        response = agent_result.response
        context_metrics = dict(agent_result.context_metrics)
    except Exception as exc:  # A broken turn must not stop the rest of the chain.
        agent_exception = _exception_text(exc)

    model_patch = ""
    model_commit: str | None = None
    try:
        # This must occur before the verifier sees any hidden test patch.
        model_patch = collect_git_diff(repo, include_untracked=True)
        model_commit = _commit_all(repo, f"ChainSWE model patch: {issue.swebench_instance_id}")
    except Exception as exc:
        runner_exception = _exception_text(exc)

    verifier, verifier_error = _run_verifier(
        issue,
        repo=repo,
        model_commit=model_commit,
        verifier_issues=verifier_issues,
    )
    if verifier_error:
        runner_exception = _join_errors(runner_exception, verifier_error)

    return ChainIssueResult(
        task=issue,
        model_patch=model_patch,
        response=response,
        agent_exception=agent_exception,
        runner_exception=runner_exception,
        elapsed_seconds=round(time.monotonic() - started, 6),
        # Test patches are intentionally never committed in the agent's
        # workspace. Their replay details live under ``verifier`` instead.
        git_commits={"model": model_commit, "test": None},
        verifier=verifier,
        context_metrics=context_metrics,
    )


def _run_verifier(
    issue: ChainSWEIssue,
    *,
    repo: Path,
    model_commit: str | None,
    verifier_issues: tuple[ChainSWEIssue, ...],
) -> tuple[VerifierResult, str | None]:
    """Replay hidden tests in an isolated worktree and verify the current task.

    The verifier starts from the model commit for *this* issue (which already
    contains all prior model commits), then replays hidden test patches from
    the beginning of the chain. A replay failure does not short-circuit later
    patch attempts or future issues; it makes the current verification fail.
    """

    if model_commit is None:
        error = "Model patch commit unavailable; verifier worktree was not created."
        return (
            VerifierResult(
                passed=False,
                exit_code=None,
                output="",
                test_cmds=issue.test_cmds,
                test_patch_applied=False,
                exception=error,
            ),
            error,
        )

    replayed: tuple[VerifierPatchResult, ...] = ()
    try:
        with _temporary_verifier_worktree(repo, model_commit) as verifier_repo:
            replayed = tuple(_apply_test_patch(verifier_repo, candidate) for candidate in verifier_issues)
            current_patch = replayed[-1]
            if not issue.test_cmds or not issue.test_cmds.strip():
                return (
                    VerifierResult(
                        passed=False,
                        exit_code=None,
                        output="No ChainSWE verifier command was provided.",
                        test_cmds=issue.test_cmds,
                        test_patch_applied=current_patch.applied,
                        test_patch_exit_code=current_patch.exit_code,
                        test_patch_output=current_patch.output,
                        exception="Missing test_cmds.",
                        replayed_test_patches=replayed,
                    ),
                    None,
                )
            process = _run_shell(issue.test_cmds, cwd=verifier_repo)
    except Exception as exc:
        error = _exception_text(exc)
        return (
            VerifierResult(
                passed=False,
                exit_code=None,
                output="",
                test_cmds=issue.test_cmds,
                test_patch_applied=False,
                exception=error,
                replayed_test_patches=replayed,
            ),
            error,
        )

    all_patches_applied = all(patch.applied for patch in replayed)
    return (
        VerifierResult(
            passed=all_patches_applied and process.returncode == 0,
            exit_code=process.returncode,
            output=process.stdout,
            test_cmds=issue.test_cmds,
            test_patch_applied=current_patch.applied,
            test_patch_exit_code=current_patch.exit_code,
            test_patch_output=current_patch.output,
            exception=(
                "One or more cumulative ChainSWE test patches failed to apply."
                if not all_patches_applied
                else None
            ),
            replayed_test_patches=replayed,
        ),
        None,
    )


def _apply_test_patch(repo: Path, issue: ChainSWEIssue) -> VerifierPatchResult:
    """Apply one test patch without letting a bad previous patch abort replay."""

    if issue.test_patch is None or not issue.test_patch.strip():
        return VerifierPatchResult(
            order=issue.order,
            swebench_instance_id=issue.swebench_instance_id,
            applied=True,
            exit_code=None,
            output="",
        )
    process = _run(["git", "apply", "-"], cwd=repo, input_text=issue.test_patch)
    return VerifierPatchResult(
        order=issue.order,
        swebench_instance_id=issue.swebench_instance_id,
        applied=process.returncode == 0,
        exit_code=process.returncode,
        output=process.stdout,
    )


class _TemporaryVerifierWorktree:
    """Create/remove one detached verifier checkout outside the agent workspace."""

    def __init__(self, repo: Path, model_commit: str) -> None:
        self.repo = repo
        self.model_commit = model_commit
        self.path: Path | None = None
        self.added = False

    def __enter__(self) -> Path:
        # ``git worktree add`` expects the target path not to exist. Make a
        # collision-free name beside the workspace, remove its empty directory,
        # then let git create the detached worktree there.
        path = Path(tempfile.mkdtemp(prefix="firstcoder-chainswe-verifier-", dir=self.repo.parent))
        path.rmdir()
        self.path = path
        _run_checked(["git", "worktree", "add", "--detach", str(path), self.model_commit], cwd=self.repo)
        self.added = True
        return path

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self.path is None:
            return False
        try:
            if self.added:
                _run_checked(["git", "worktree", "remove", "--force", str(self.path)], cwd=self.repo)
        finally:
            # If git failed before registering the worktree, or if a tool left
            # artifacts behind, never leave verifier files near the checkout.
            shutil.rmtree(self.path, ignore_errors=True)
        return False


def _temporary_verifier_worktree(repo: Path, model_commit: str) -> _TemporaryVerifierWorktree:
    return _TemporaryVerifierWorktree(repo, model_commit)


def _run_shell(command: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        executable="/bin/sh",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _commit_all(repo: Path, message: str) -> str:
    _run_checked(["git", "add", "-A"], cwd=repo)
    _run_checked(
        [
            "git",
            "-c",
            "user.name=FirstCoder ChainSWE",
            "-c",
            "user.email=chainswe@firstcoder.local",
            "commit",
            "--allow-empty",
            "-m",
            message,
        ],
        cwd=repo,
    )
    return _run_checked(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _run(
    command: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _run_checked(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    process = _run(command, cwd=cwd)
    if process.returncode != 0:
        raise RuntimeError(f"Command failed ({process.returncode}): {' '.join(command)}\n{process.stdout}")
    return process


def _create_provider(
    *,
    provider_name: str | None,
    model: str | None,
    project_root: Path,
) -> ChatProvider:
    config = load_config(provider_name, project_root=project_root)
    if model:
        config = replace(config, env={**config.env, "FIRSTCODER_MODEL": model})
    return create_provider_from_config(config)


def _require_data_root_outside_workspace(workspace: Path, data_root: Path) -> None:
    if data_root == workspace or workspace in data_root.parents:
        raise ValueError("ChainSWE data_root must be outside the benchmark workspace; git clean would remove it.")


def _issue_to_dict(issue: ChainSWEIssue) -> dict[str, Any]:
    return {
        "order": issue.order,
        "swebench_instance_id": issue.swebench_instance_id,
        "problem_statement": issue.problem_statement,
        "fail_to_pass": list(issue.fail_to_pass),
        "pass_to_pass": list(issue.pass_to_pass),
        "test_patch": issue.test_patch,
        "test_cmds": issue.test_cmds,
    }


def _chain_to_dict(chain: ChainSWEChain) -> dict[str, Any]:
    return {
        "continuous_id": chain.continuous_id,
        "repo": chain.repo,
        "base_commit": chain.base_commit,
        "docker_image": chain.docker_image,
        "issues": [_issue_to_dict(issue) for issue in chain.issues],
    }


def _exception_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _join_errors(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one persistent FirstCoder session over a ChainSWE chain.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--chains", help="Path to the local ChainSWE JSONL dataset.")
    source.add_argument(
        "--chain-stdin",
        action="store_true",
        help="Read exactly one selected official ChainSWE JSON record from standard input.",
    )
    parser.add_argument("--chain-id", help="continuous_id of the chain to run with --chains.")
    parser.add_argument("--workspace", required=True, help="Prepared git checkout for the selected chain.")
    parser.add_argument(
        "--data-root",
        default=".firstcoder-chainswe-data",
        help="Session data directory. Relative paths resolve beside the workspace, never inside it.",
    )
    parser.add_argument("--provider", default=None, help="FirstCoder provider override.")
    parser.add_argument("--model", default=None, help="FirstCoder model override.")
    parser.add_argument("--max-tool-rounds", type=int, default=None, help="Override the SWE-lite tool round limit.")
    parser.add_argument("--summary-out", default=None, help="Optional JSON summary output path.")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not reset and clean workspace to chain.base_commit first.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.chain_stdin:
        chain = _load_chain_from_stdin()
    else:
        if not args.chain_id:
            build_parser().error("--chain-id is required when using --chains")
        chain = select_chain(load_chains_jsonl(args.chains), args.chain_id)
    workspace = Path(args.workspace).resolve()
    data_root = Path(args.data_root)
    if not data_root.is_absolute():
        data_root = workspace.parent / data_root
    data_root = data_root.resolve()

    # Construct the app after reset so AGENTS.md and other repo-local context
    # are read from exactly the base checkout used for the benchmark.
    if not args.no_reset:
        reset_chain_workspace(workspace, chain)
    agent = FirstCoderChainAgent(
        workspace=workspace,
        data_root=data_root,
        provider_name=args.provider,
        model=args.model,
        session_id=f"chainswe-{_safe_session_id(chain.continuous_id)}",
        max_tool_rounds=args.max_tool_rounds,
    )
    summary_out = Path(args.summary_out).resolve() if args.summary_out else None
    result = run_chain(
        chain,
        workspace=workspace,
        agent=agent,
        reset_workspace=False,
        progress_out=summary_out,
    )
    payload = result.to_dict()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if summary_out:
        _write_json_atomic(summary_out, payload)
    print(rendered)
    # Failed benchmark tasks are observations, not runner process failures.
    return 0


def _safe_session_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value) or "chain"


def _load_chain_from_stdin() -> ChainSWEChain:
    """Consume one selected chain record before constructing the benchmark agent."""

    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("--chain-stdin requires one JSON ChainSWE record on standard input")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--chain-stdin received invalid JSON") from exc
    return parse_chain_record(record)


if __name__ == "__main__":
    raise SystemExit(main())
