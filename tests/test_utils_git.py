"""utils/git tests."""

from __future__ import annotations

from firstcoder.utils import git as git_utils
from firstcoder.utils.sandbox import PathSandbox


def test_run_git_filters_sensitive_environment(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return git_utils.subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("FIRSTCODER_VISIBLE_TEST_FLAG", "visible")

    result = git_utils.run_git(PathSandbox(tmp_path), ["status", "--short"])

    assert result.returncode == 0
    assert "GITHUB_TOKEN" not in captured["env"]
    assert captured["env"]["FIRSTCODER_VISIBLE_TEST_FLAG"] == "visible"
