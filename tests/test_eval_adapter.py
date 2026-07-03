from pathlib import Path
import subprocess

import pytest

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.eval.adapter import FirstCoderCodingAgentAdapter
from firstcoder.providers.base import ChatProvider
from firstcoder.eval.tasks import CodingTask
from firstcoder.providers.types import ChatRequest, ToolCall
from firstcoder.providers.types import ChatResponse


class FakeLoop:
    def __init__(self):
        self.messages: list[str] = []

    def run_user_turn(self, content: str) -> ChatResponse:
        self.messages.append(content)
        return ChatResponse(
            provider="fake",
            model="fake-model",
            content="done",
            finish_reason="stop",
        )


class FileWritingLoop:
    def __init__(self, repo: Path):
        self.repo = repo

    def run_user_turn(self, content: str) -> ChatResponse:
        (self.repo / "new_module.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")
        return ChatResponse(
            provider="fake",
            model="fake-model",
            content="done",
            finish_reason="stop",
        )


class FakeProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(provider=self.name, model=self.model, content="done", finish_reason="stop")


class PatchProvider(ChatProvider):
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
        if self.calls == 1:
            return ChatResponse(
                provider=self.name,
                model=self.model,
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_patch",
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "*** Begin Patch\n"
                                "*** Add File: fixed.py\n"
                                "+VALUE = 42\n"
                                "*** End Patch"
                            )
                        },
                    )
                ],
            )
        return ChatResponse(provider=self.name, model=self.model, content="done", finish_reason="stop")


def init_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, text=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("benchmark repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, text=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, text=True, capture_output=True)


def test_adapter_builds_benchmark_prompt(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    loop = FakeLoop()
    adapter = FirstCoderCodingAgentAdapter(
        model_name_or_path="firstcoder-test",
        loop_factory=lambda task, session_root: loop,
        session_root=tmp_path / "sessions",
    )
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
        base_commit="abc123",
    )

    result = adapter.run_task(task)

    assert result.instance_id == "sympy__sympy-20590"
    assert result.model_name_or_path == "firstcoder-test"
    assert "Fix the issue." in loop.messages[0]
    assert "Return by editing files" in loop.messages[0]
    assert result.raw_response == "done"


def test_default_loop_factory_keeps_session_outside_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    session_root = tmp_path / "sessions"
    adapter = FirstCoderCodingAgentAdapter(
        session_root=session_root,
        provider_factory=lambda provider_name: FakeProvider(),
    )
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    loop = adapter._create_loop(task, session_root / task.instance_id)

    assert loop.session.store.root == session_root / task.instance_id
    assert repo not in loop.session.store.root.parents
    assert loop.session.mode == "bypass"
    assert "write" in loop.session.tool_registry.names()
    assert loop.limits == AgentLoopLimits.swe_lite()


def test_default_loop_factory_auto_allows_repo_writes_for_benchmarks(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    adapter = FirstCoderCodingAgentAdapter(
        session_root=tmp_path / "sessions",
        provider_factory=lambda provider_name: PatchProvider(),
    )
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Create the fix.",
    )

    result = adapter.run_task(task)

    assert result.raw_response == "done"
    assert "diff --git a/fixed.py b/fixed.py" in result.model_patch
    assert "+VALUE = 42" in result.model_patch


def test_relative_session_root_is_resolved_outside_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    loop = FakeLoop()
    seen_session_roots: list[Path] = []

    def make_loop(task: CodingTask, session_root: Path) -> FakeLoop:
        seen_session_roots.append(session_root)
        return loop

    monkeypatch.chdir(repo)
    adapter = FirstCoderCodingAgentAdapter(loop_factory=make_loop)
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    adapter.run_task(task)

    assert seen_session_roots == [tmp_path / ".firstcoder-eval" / "sympy__sympy-20590"]
    assert repo not in seen_session_roots[0].parents


def test_session_root_inside_repo_is_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    adapter = FirstCoderCodingAgentAdapter(session_root="repo/.firstcoder-eval")
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    with pytest.raises(ValueError, match="outside the task repository"):
        adapter.run_task(task)


def test_instance_id_is_sanitized_for_session_directory(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    loop = FakeLoop()
    seen_session_roots: list[Path] = []

    def make_loop(task: CodingTask, session_root: Path) -> FakeLoop:
        seen_session_roots.append(session_root)
        return loop

    adapter = FirstCoderCodingAgentAdapter(
        session_root=tmp_path / "sessions",
        loop_factory=make_loop,
    )
    task = CodingTask(
        instance_id="../sympy/sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    result = adapter.run_task(task)

    assert result.instance_id == "../sympy/sympy-20590"
    expected_root = tmp_path / "sessions" / "__sympy_sympy-20590"
    assert seen_session_roots == [expected_root]
    assert result.transcript_path == expected_root / "sessions" / "__sympy_sympy-20590.jsonl"
    assert expected_root in result.transcript_path.parents


def test_default_loop_factory_sanitizes_internal_session_id(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    adapter = FirstCoderCodingAgentAdapter(
        session_root=tmp_path / "sessions",
        provider_factory=lambda provider_name: FakeProvider(),
    )
    task = CodingTask(
        instance_id="../sympy/sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    loop = adapter._create_loop(task, tmp_path / "sessions" / "__sympy_sympy-20590")

    assert loop.session.session_id == "__sympy_sympy-20590"
    assert loop.session.store._session_path(loop.session.session_id) == (
        tmp_path / "sessions" / "__sympy_sympy-20590" / "sessions" / "__sympy_sympy-20590.jsonl"
    )


def test_adapter_patch_includes_untracked_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    adapter = FirstCoderCodingAgentAdapter(
        session_root=tmp_path / "sessions",
        loop_factory=lambda task, session_root: FileWritingLoop(repo),
    )
    task = CodingTask(
        instance_id="sympy__sympy-20590",
        repo_path=repo,
        problem_statement="Fix the issue.",
    )

    result = adapter.run_task(task)

    assert "diff --git a/new_module.py b/new_module.py" in result.model_patch
    assert "+NEW_VALUE = 3" in result.model_patch
