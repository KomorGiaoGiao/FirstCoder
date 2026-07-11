from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from benchmark.chainswe.models import ChainSWEChain, ChainSWEIssue
from benchmark.chainswe.runner import (
    ChainAgentResponse,
    FirstCoderChainAgent,
    _load_chain_from_stdin,
    build_parser,
    build_task_prompt,
    reset_chain_workspace,
    run_chain,
)
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


class RecordingAgent:
    def __init__(self, workspace: Path, *, fail_on_call: int | None = None) -> None:
        self.workspace = workspace
        self.fail_on_call = fail_on_call
        self.prompts: list[str] = []
        self.seen_markers: list[tuple[bool, bool]] = []

    def run_task(self, prompt: str) -> ChainAgentResponse:
        self.prompts.append(prompt)
        self.seen_markers.append(
            (
                (self.workspace / "verifier-one.txt").exists(),
                (self.workspace / "verifier-two.txt").exists(),
            )
        )
        call = len(self.prompts)
        if self.fail_on_call == call:
            raise RuntimeError(f"agent broke on {call}")
        (self.workspace / "model.txt").write_text(
            (self.workspace / "model.txt").read_text(encoding="utf-8") + f"model-{call}\n",
            encoding="utf-8",
        )
        return ChainAgentResponse(response=f"response-{call}", context_metrics={"turn": call})


class FakeProvider(ChatProvider):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        return ChatResponse(provider=self.name, model=self.model, content="done", finish_reason="stop")


def _issue(
    order: int,
    *,
    test_patch: str | None = None,
    test_cmds: str = "test -f model.txt",
) -> ChainSWEIssue:
    return ChainSWEIssue(
        order=order,
        swebench_instance_id=f"example__repo-{order}",
        problem_statement=f"Implement public behavior {order}.",
        fail_to_pass=(f"test_hidden_{order}",),
        pass_to_pass=(f"test_regression_{order}",),
        test_patch=test_patch,
        test_cmds=test_cmds,
    )


def _chain(base_commit: str, *issues: ChainSWEIssue) -> ChainSWEChain:
    return ChainSWEChain(
        continuous_id="chain-1",
        repo="example/repo",
        base_commit=base_commit,
        docker_image="chainswe/example:latest",
        issues=issues,
    )


def _new_file_patch(name: str, contents: str) -> str:
    return (
        f"diff --git a/{name} b/{name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{name}\n"
        f"@@ -0,0 +1 @@\n+{contents}\n"
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    (repo / "model.txt").write_text("base\n", encoding="utf-8")
    _git(["init"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["add", "model.txt"], repo)
    _git(["commit", "-m", "base"], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )


def test_run_chain_reuses_one_agent_hides_verifier_data_and_uses_order(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    later = _issue(
        2,
        test_patch=_new_file_patch("verifier-two.txt", "two"),
        test_cmds="test -f verifier-one.txt && test -f verifier-two.txt && test -f model.txt",
    )
    first = _issue(
        1,
        test_patch=_new_file_patch("verifier-one.txt", "one"),
        test_cmds="test -f verifier-one.txt && test -f model.txt",
    )
    agent = RecordingAgent(repo)

    result = run_chain(_chain(base, later, first), workspace=repo, agent=agent)

    assert [item.task.order for item in result.results] == [1, 2]
    assert len(agent.prompts) == 2
    assert all("Implement public behavior" in prompt for prompt in agent.prompts)
    assert all("verifier-one.txt" not in prompt and "test_hidden" not in prompt for prompt in agent.prompts)
    assert all("test -f" not in prompt for prompt in agent.prompts)
    # All hidden test material lives only in short-lived verifier worktrees.
    # The persistent agent checkout never receives even prior issue patches.
    assert agent.seen_markers == [(False, False), (False, False)]
    assert [item.response for item in result.results] == ["response-1", "response-2"]
    assert [item.context_metrics for item in result.results] == [{"turn": 1}, {"turn": 2}]
    assert result.per_bug_success == 1.0
    assert result.full_chain_success is True


def test_run_chain_continues_after_agent_and_verifier_failures(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    issues = (
        _issue(1, test_cmds="test -f model.txt"),
        _issue(2, test_cmds="exit 7"),
        _issue(3, test_cmds="test -f model.txt"),
    )
    agent = RecordingAgent(repo, fail_on_call=2)

    result = run_chain(_chain(base, *issues), workspace=repo, agent=agent)

    assert len(agent.prompts) == 3
    assert [item.passed for item in result.results] == [True, False, True]
    assert result.results[1].agent_exception == "RuntimeError: agent broke on 2"
    assert result.results[1].verifier.exit_code == 7
    assert result.results[2].response == "response-3"
    assert result.passed_bug_count == 2
    assert result.per_bug_success == 2 / 3
    assert result.full_chain_success is False


def test_run_chain_writes_safe_progress_snapshot_after_each_issue(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    progress = tmp_path / "run" / "summary.json"

    class ProgressInspectingAgent(RecordingAgent):
        snapshots: list[dict] = []

        def run_task(self, prompt: str) -> ChainAgentResponse:
            if self.prompts:
                self.snapshots.append(json.loads(progress.read_text(encoding="utf-8")))
            return super().run_task(prompt)

    first = _issue(
        1,
        test_patch=_new_file_patch("hidden-first.txt", "first"),
        test_cmds="test -f hidden-first.txt && test -f model.txt",
    )
    second = _issue(2, test_cmds="test -f model.txt")
    agent = ProgressInspectingAgent(repo)

    result = run_chain(
        _chain(base, first, second),
        workspace=repo,
        agent=agent,
        progress_out=progress,
    )

    assert result.full_chain_success is True
    assert len(agent.snapshots) == 1
    snapshot = agent.snapshots[0]
    assert snapshot["run_status"] == "running"
    assert snapshot["completed_issue_count"] == 1
    assert snapshot["total_issue_count"] == 2
    assert snapshot["passed_issue_count"] == 1
    assert snapshot["results"] == [
        {
            "order": 1,
            "swebench_instance_id": "example__repo-1",
            "passed": True,
            "elapsed_seconds": snapshot["results"][0]["elapsed_seconds"],
            "agent_status": "completed",
            "runner_status": "completed",
            "verification": {"status": "passed", "exit_code": 0},
            "context_metrics": {"turn": 1},
        }
    ]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "hidden-first.txt" not in serialized
    assert "test -f" not in serialized


def test_run_chain_commits_model_and_keeps_test_patch_out_of_agent_workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    issue = _issue(
        1,
        test_patch=_new_file_patch("verifier-one.txt", "one"),
        test_cmds="test -f verifier-one.txt && test -f model.txt",
    )

    [result] = run_chain(_chain(base, issue), workspace=repo, agent=RecordingAgent(repo)).results

    assert "model-1" in result.model_patch
    assert result.git_commits["model"] is not None
    assert result.git_commits["test"] is None
    model_show = _git(["show", "--format=", result.git_commits["model"]], repo).stdout
    assert "model-1" in model_show
    assert "verifier-one.txt" not in model_show
    assert result.verifier.test_patch_applied is True
    assert result.verifier.replayed_test_patches[0].applied is True
    assert not (repo / "verifier-one.txt").exists()
    assert not list(tmp_path.glob("firstcoder-chainswe-verifier-*"))


def test_run_chain_reset_restores_base_and_clean_checkout(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "model.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("remove me\n", encoding="utf-8")
    agent = RecordingAgent(repo)

    result = run_chain(_chain(base, _issue(1)), workspace=repo, agent=agent)

    assert result.reset_workspace is True
    assert "dirty" not in result.results[0].model_patch
    assert not (repo / "untracked.txt").exists()
    assert "base\nmodel-1\n" == (repo / "model.txt").read_text(encoding="utf-8")


def test_run_chain_no_reset_keeps_existing_checkout_state(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "model.txt").write_text("existing\n", encoding="utf-8")

    [result] = run_chain(
        _chain(base, _issue(1)),
        workspace=repo,
        agent=RecordingAgent(repo),
        reset_workspace=False,
    ).results

    assert "existing" in result.model_patch
    assert (repo / "model.txt").read_text(encoding="utf-8") == "existing\nmodel-1\n"


def test_verifier_replays_prior_patches_for_each_issue_without_leaking_them(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    first = _issue(
        1,
        test_patch=_new_file_patch("prior-verifier.txt", "prior"),
        test_cmds="test -f prior-verifier.txt && test -f model.txt",
    )
    second = _issue(
        2,
        test_patch=_new_file_patch("current-verifier.txt", "current"),
        test_cmds="test -f prior-verifier.txt && test -f current-verifier.txt && test -f model.txt",
    )
    agent = RecordingAgent(repo)

    result = run_chain(_chain(base, first, second), workspace=repo, agent=agent)

    assert [item.passed for item in result.results] == [True, True]
    assert [patch.order for patch in result.results[1].verifier.replayed_test_patches] == [1, 2]
    assert all(patch.applied for patch in result.results[1].verifier.replayed_test_patches)
    assert agent.seen_markers == [(False, False), (False, False)]
    assert not (repo / "prior-verifier.txt").exists()
    assert not (repo / "current-verifier.txt").exists()


def test_verifier_records_bad_prior_patch_and_still_attempts_current_patch(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    broken = _issue(1, test_patch="this is not a git patch\n")
    current = _issue(
        2,
        test_patch=_new_file_patch("current-verifier.txt", "current"),
        test_cmds="test -f current-verifier.txt && test -f model.txt",
    )

    result = run_chain(_chain(base, broken, current), workspace=repo, agent=RecordingAgent(repo))

    second_verifier = result.results[1].verifier
    assert [patch.applied for patch in second_verifier.replayed_test_patches] == [False, True]
    assert second_verifier.test_patch_applied is True
    assert second_verifier.passed is False
    assert "cumulative" in (second_verifier.exception or "")
    assert not (repo / "current-verifier.txt").exists()


def test_reset_chain_workspace_can_be_called_without_running_agent(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "model.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")

    reset_chain_workspace(repo, _chain(base, _issue(1)))

    assert (repo / "model.txt").read_text(encoding="utf-8") == "base\n"
    assert not (repo / "scratch.txt").exists()


def test_result_is_json_serializable_and_contains_required_trace_fields(tmp_path: Path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    result = run_chain(_chain(base, _issue(1)), workspace=repo, agent=RecordingAgent(repo))
    payload = json.loads(json.dumps(result.to_dict()))
    row = payload["results"][0]

    assert payload["per_bug_success"] == 1.0
    assert payload["per_bug_success_count"] == 1
    assert payload["full_chain_success"] is True
    assert set(row) >= {
        "task",
        "model_patch",
        "response",
        "agent_exception",
        "elapsed_seconds",
        "git_commits",
        "verifier",
        "context_metrics",
    }
    assert row["task"]["test_patch"] is None
    assert row["verifier"]["passed"] is True


def test_task_prompt_only_includes_problem_statement_and_public_instructions():
    prompt = build_task_prompt("Fix the documented parser behavior.")

    assert "Fix the documented parser behavior." in prompt
    assert "test patches" in prompt  # public guardrail, not task-specific verifier data
    assert "Do not\nmodify test files" in prompt
    assert "FAIL_TO_PASS" not in prompt


def test_firstcoder_chain_agent_uses_bypass_no_network_tools_and_swe_limits(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    provider = FakeProvider()
    agent = FirstCoderChainAgent(
        workspace=repo,
        data_root=tmp_path / "sessions",
        provider=provider,
        max_tool_rounds=11,
    )

    assert agent.app.current_session.session.mode == "bypass"
    assert agent.app.current_session.session.sandbox_access.unrestricted is True
    assert {tool.name for tool in agent.app.chat_runner.tools} >= {"write", "shell"}
    assert {tool.name for tool in agent.app.chat_runner.tools}.isdisjoint({"fetch", "web_search"})
    assert agent.app.chat_runner.limits == AgentLoopLimits.swe_lite().with_max_tool_rounds(11)

    session = agent.app.current_session.session
    message_id = session.append_user_message("A newly ordered ChainSWE issue.")
    boundary = session.tool_registry.execute(
        "task_boundary",
        {"decision": "new", "basis_message_id": message_id},
    )
    assert boundary.ok is True
    assert boundary.data["required_stable_count"] == 1

    initial_session = session
    first = agent.run_task(build_task_prompt("First public issue."))
    second = agent.run_task(build_task_prompt("Second public issue."))

    assert agent.app.current_session.session is initial_session
    assert provider.calls == 2
    assert first.response == second.response == "done"


def test_parser_exposes_chain_runner_options():
    args = build_parser().parse_args(
        [
            "--chains",
            "chains.jsonl",
            "--chain-id",
            "chain-1",
            "--workspace",
            "checkout",
            "--data-root",
            "data",
            "--provider",
            "openai-compatible",
            "--model",
            "gpt-test",
            "--max-tool-rounds",
            "12",
            "--summary-out",
            "summary.json",
            "--no-reset",
        ]
    )

    assert args.chain_id == "chain-1"
    assert args.no_reset is True
    assert args.max_tool_rounds == 12


def test_parser_accepts_selected_chain_from_stdin_without_dataset_path():
    args = build_parser().parse_args(["--chain-stdin", "--workspace", "checkout"])

    assert args.chain_stdin is True
    assert args.chains is None
    assert args.chain_id is None


def test_load_chain_from_stdin_parses_one_official_record(monkeypatch):
    record = {
        "continuous_id": "stdin-chain",
        "repo": "org/repo",
        "base_commit": "abc123",
        "docker_image": "example/image",
        "bug_fixes": [
            {
                "order": 1,
                "swebench_instance_id": "org__repo-1",
                "problem_statement": "Fix it.",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "test_patch": "hidden patch",
                "test_cmds": "hidden command",
            }
        ],
    }
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"read": lambda self: json.dumps(record)})())

    chain = _load_chain_from_stdin()

    assert chain.continuous_id == "stdin-chain"
    assert chain.issues[0].test_patch == "hidden patch"
