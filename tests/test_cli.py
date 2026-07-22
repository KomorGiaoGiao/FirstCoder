from pathlib import Path

from dataclasses import dataclass, field

import firstcoder.cli as cli
from firstcoder.cli import CliConfig, main, read_message, run_repl


@dataclass
class FakeResponse:
    content: str
    finish_reason: str = "stop"


@dataclass
class FakePending:
    id: str
    kind: str
    question: str
    options: list[object] = field(default_factory=list)


@dataclass
class FakeOption:
    id: str
    label: str


@dataclass
class FakeChatRunner:
    replies: list[FakeResponse]
    pending_after_turn: FakePending | None = None
    pending_after_resume: list[FakePending | None] | None = None
    turns: list[str] = field(default_factory=list)
    resumes: list[tuple[str, str]] = field(default_factory=list)
    last_pending_input: FakePending | None = None

    def run_user_turn(self, content: str) -> FakeResponse:
        self.turns.append(content)
        self.last_pending_input = self.pending_after_turn
        return self.replies.pop(0)

    def resume_with_user_input(self, request_id: str, answer: str) -> FakeResponse:
        self.resumes.append((request_id, answer))
        if self.pending_after_resume is None:
            self.last_pending_input = None
        else:
            self.last_pending_input = self.pending_after_resume.pop(0)
        return self.replies.pop(0)


class FakeCliApp:
    def __init__(self) -> None:
        self.run_count = 0

    def run(self) -> None:
        self.run_count += 1


def test_read_message_prefers_argument_over_stdin():
    assert read_message("hello", stdin_text="ignored") == "hello"


def test_read_message_reads_stdin_when_message_missing():
    assert read_message(None, stdin_text="hello from stdin\n") == "hello from stdin"


def test_main_runs_single_message_with_injected_runner(tmp_path: Path, capsys):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--data-root",
            str(tmp_path / ".fc"),
            "--session-id",
            "cli_test",
            "--message",
            "solve it",
        ],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "done\n"
    assert seen == [
        CliConfig(
            project_root=tmp_path,
            data_root=tmp_path / ".fc",
            session_id="cli_test",
            provider_name=None,
            message="solve it",
            max_tool_rounds=None,
            benchmark=False,
        )
    ]


def test_main_parses_model_reference_for_single_message(tmp_path: Path):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    assert (
        main(
            [
                "--project",
                str(tmp_path),
                "--model",
                "yuren/gpt-5.6-terra",
                "--message",
                "hello",
            ],
            runner=fake_runner,
        )
        == 0
    )

    assert seen[0].model_spec == "yuren/gpt-5.6-terra"


def test_main_returns_error_for_empty_message(tmp_path: Path, capsys):
    exit_code = main(["--project", str(tmp_path)], stdin_text="")

    assert exit_code == 2
    assert "message is required" in capsys.readouterr().err


def test_main_config_path_prints_global_and_project_paths(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    exit_code = main(["--project", str(tmp_path), "config", "path"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"global: {tmp_path / 'xdg' / 'firstcoder' / 'config.toml'}" in output
    assert f"project: {tmp_path / 'firstcoder.toml'}" in output


def test_main_config_init_creates_global_config(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    exit_code = main(["config", "init"])

    config_path = tmp_path / "xdg" / "firstcoder" / "config.toml"
    assert exit_code == 0
    assert config_path.exists()
    assert "api_key_env" in config_path.read_text(encoding="utf-8")
    assert f"created: {config_path}" in capsys.readouterr().out


def test_main_mcp_add_remote_accepts_bearer_token_environment_variable(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    exit_code = main(
        [
            "mcp",
            "add",
            "github",
            "--url",
            "https://example.test/mcp",
            "--bearer-token-env-var",
            "GITHUB_PAT_TOKEN",
        ]
    )

    config = (tmp_path / "xdg" / "firstcoder" / "config.toml").read_text(encoding="utf-8")
    assert exit_code == 0
    assert 'bearer_token_env_var = "GITHUB_PAT_TOKEN"' in config
    assert "Added remote MCP server: github" in capsys.readouterr().out


def test_main_config_init_refuses_to_overwrite_without_force(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config_path = tmp_path / "xdg" / "firstcoder" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("existing", encoding="utf-8")

    exit_code = main(["config", "init"])

    assert exit_code == 1
    assert config_path.read_text(encoding="utf-8") == "existing"
    assert "already exists" in capsys.readouterr().err


def test_main_config_show_uses_project_config_without_leaking_key(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)
    monkeypatch.setenv("YURENAPI_API_KEY", "secret-key")
    (tmp_path / "firstcoder.toml").write_text(
        "\n".join(
            [
                'default_model = "yurenapi/gpt-5.5"',
                "[providers.yurenapi]",
                'type = "openai-compatible"',
                'base_url = "https://yurenapi.cn/v1"',
                'api_key_env = "YURENAPI_API_KEY"',
                "parallel_tool_calls = true",
                '[models."yurenapi/gpt-5.5"]',
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--project", str(tmp_path), "config", "show"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "provider: yurenapi" in output
    assert "model: yurenapi/gpt-5.5" in output
    assert "base_url: https://yurenapi.cn/v1" in output
    assert "parallel_tool_calls: true" in output
    assert "secret-key" not in output


def test_main_config_show_lists_catalog_refs_without_secrets_or_state(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("FIRSTCODER_PROVIDER", raising=False)
    monkeypatch.setenv("YURENAPI_API_KEY", "secret-key")
    (tmp_path / "firstcoder.toml").write_text(
        "\n".join(
            [
                'default_model = "yuren/gpt-5.6-terra"',
                "[providers.yuren]",
                'type = "openai-compatible"',
                'base_url = "https://yurenapi.cn/v1"',
                'api_key_env = "YURENAPI_API_KEY"',
                '[models."yuren/gpt-5.6-terra"]',
                'label = "Yuren Terra"',
                '[models."yuren/gpt-5.6-terra".request]',
                'extra_body = { secret = "do-not-print" }',
                '[models."openai/gpt-5.5"]',
                'label = "OpenAI"',
                "[providers.openai]",
                'type = "openai-compatible"',
                'api_key_env = "OPENAI_API_KEY"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".firstcoder" / "model_state.json").parent.mkdir(parents=True)
    (tmp_path / ".firstcoder" / "model_state.json").write_text(
        '{"last_selected":"yuren/gpt-5.6-terra","recent":["openai/gpt-5.5"]}',
        encoding="utf-8",
    )

    assert main(["--project", str(tmp_path), "config", "show"]) == 0
    output = capsys.readouterr().out
    assert "default_model: yuren/gpt-5.6-terra" in output
    assert "  - yuren/gpt-5.6-terra (Yuren Terra)" in output
    assert "  - openai/gpt-5.5 (OpenAI)" in output
    assert "YURENAPI_API_KEY" not in output
    assert "OPENAI_API_KEY" not in output
    assert "secret-key" not in output
    assert "do-not-print" not in output
    assert "model_state.json" not in output


def test_main_tui_runs_textual_app(monkeypatch, tmp_path: Path):
    app = FakeCliApp()
    seen: list[CliConfig] = []

    def fake_create_cli_app(config: CliConfig):
        seen.append(config)
        return app

    monkeypatch.setattr(cli, "create_cli_app", fake_create_cli_app)

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--data-root",
            str(tmp_path / ".fc"),
            "--session-id",
            "tui_test",
            "--tui",
            "--max-tool-rounds",
            "3",
        ]
    )

    assert exit_code == 0
    assert app.run_count == 1
    assert seen == [
        CliConfig(
            project_root=tmp_path,
            data_root=tmp_path / ".fc",
            session_id="tui_test",
            provider_name=None,
            message="",
            max_tool_rounds=3,
            benchmark=False,
        )
    ]


def test_run_repl_sends_multiple_user_messages(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("first reply"),
            FakeResponse("second reply"),
        ]
    )

    run_repl(runner, ["hello", "continue"])

    assert runner.turns == ["hello", "continue"]
    assert capsys.readouterr().out == "FirstCoder> first reply\nFirstCoder> second reply\n"


def test_run_repl_routes_next_line_to_pending_permission(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow?"),
    )

    run_repl(runner, ["write file", "allow_once"])

    assert runner.turns == ["write file"]
    assert runner.resumes == [("perm_1", "allow_once")]
    assert capsys.readouterr().out == ("FirstCoder> need permission\n" "Permission> Allow?\n" "Choose:\n" "  1. Deny\n" "  2. Allow once\n" "  3. Allow always for same scope\n" "FirstCoder> done\n")


def test_run_repl_accepts_human_permission_aliases(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow?"),
    )

    run_repl(runner, ["write file", "always"])

    assert runner.resumes == [("perm_1", "allow_always_same_scope")]
    assert "3. Allow always for same scope" in capsys.readouterr().out


def test_run_repl_accepts_permission_rejection_feedback(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow?"),
    )

    run_repl(runner, ["write file", "reject: keep the title"])

    assert runner.resumes == [("perm_1", "reject_with_feedback: keep the title")]


def test_run_repl_renders_and_accepts_pending_permission_options(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(
            id="perm_1",
            kind="permission_confirmation",
            question="Allow?",
            options=[
                FakeOption(id="deny", label="Deny"),
                FakeOption(id="allow_once", label="Allow once"),
                FakeOption(id="allow_always_same_scope", label="Allow always"),
            ],
        ),
    )

    run_repl(runner, ["write file", "Allow always"])

    assert runner.resumes == [("perm_1", "allow_always_same_scope")]
    output = capsys.readouterr().out
    assert "3. Allow always (allow_always_same_scope)" in output


def test_run_repl_reprompts_unknown_permission_choice(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need permission", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow?"),
    )

    run_repl(runner, ["write file", "maybe", "2"])

    assert runner.resumes == [("perm_1", "allow_once")]
    output = capsys.readouterr().out
    assert "Unknown permission choice: maybe" in output
    assert "Please choose 1, 2, 3." in output


def test_run_repl_auto_approves_repeated_permissions(capsys):
    runner = FakeChatRunner(
        replies=[
            FakeResponse("need first", finish_reason="waiting_for_user_input"),
            FakeResponse("need second", finish_reason="waiting_for_user_input"),
            FakeResponse("done"),
        ],
        pending_after_turn=FakePending(id="perm_1", kind="permission_confirmation", question="Allow first?"),
        pending_after_resume=[FakePending(id="perm_2", kind="permission_confirmation", question="Allow second?"), None],
    )

    run_repl(runner, ["work"], auto_approve=True)

    assert runner.turns == ["work"]
    assert runner.resumes == [("perm_1", "allow_once"), ("perm_2", "allow_once")]
    assert "Auto-approve> allow_once" in capsys.readouterr().out


def test_main_parses_max_tool_rounds_for_single_message(tmp_path: Path):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--message",
            "solve it",
            "--max-tool-rounds",
            "80",
        ],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert seen[0].max_tool_rounds == 80


def test_main_parses_benchmark_mode_for_single_message(tmp_path: Path):
    seen: list[CliConfig] = []

    def fake_runner(config: CliConfig) -> str:
        seen.append(config)
        return "done"

    exit_code = main(
        [
            "--project",
            str(tmp_path),
            "--data-root",
            str(tmp_path / ".fc-bench"),
            "--session-id",
            "terminal_task",
            "--message",
            "solve it",
            "--benchmark",
            "--max-tool-rounds",
            "120",
        ],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert seen == [
        CliConfig(
            project_root=tmp_path,
            data_root=tmp_path / ".fc-bench",
            session_id="terminal_task",
            provider_name=None,
            message="solve it",
            max_tool_rounds=120,
            benchmark=True,
        )
    ]


def test_run_benchmark_turn_uses_harbor_runtime_without_eval_adapter(tmp_path: Path, monkeypatch):
    class FakeSession:
        require_prewrite_review = True

        def __init__(self) -> None:
            self.permission_mode = None

        def set_permission_mode(self, mode):
            self.permission_mode = mode

    class FakeCurrentSession:
        def __init__(self) -> None:
            self.session = FakeSession()

        def set_permission_mode(self, mode):
            self.session.set_permission_mode(mode)

    class FakeRunner:
        def __init__(self) -> None:
            self.limits = None

        def run_user_turn(self, message: str) -> FakeResponse:
            assert message == "solve it"
            return FakeResponse("done")

    class FakeApp:
        def __init__(self) -> None:
            self.current_session = FakeCurrentSession()
            self.chat_runner = FakeRunner()

    app = FakeApp()
    monkeypatch.setattr(cli, "create_cli_app", lambda config: app)

    output = cli.run_benchmark_turn(
        CliConfig(
            project_root=tmp_path,
            data_root=tmp_path / ".fc-bench",
            session_id="harbor-task",
            provider_name=None,
            message="solve it",
            max_tool_rounds=120,
            benchmark=True,
        )
    )

    assert output == "done"
    assert str(app.current_session.session.permission_mode) == "bypass"
    assert app.current_session.session.require_prewrite_review is False
    assert app.chat_runner.limits.max_tool_rounds == 120


def test_mcp_add_list_and_remove_manage_global_configuration(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert main(["mcp", "add", "everything", "npx", "-y", "@modelcontextprotocol/server-everything"]) == 0
    assert main(["mcp", "add", "parallel", "--url", "https://search.parallel.ai/mcp"]) == 0
    assert main(["mcp", "list"]) == 0

    output = capsys.readouterr().out
    assert "Added local MCP server: everything" in output
    assert "Added remote MCP server: parallel" in output
    assert "everything local npx -y @modelcontextprotocol/server-everything enabled" in output
    assert "parallel remote https://search.parallel.ai/mcp enabled" in output

    assert main(["mcp", "remove", "everything"]) == 0
    assert "Removed MCP server: everything" in capsys.readouterr().out


def test_mcp_add_validates_remote_and_key_value_options(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert main(["mcp", "add", "remote", "--url", "https://example.test/mcp", "--env", "KEY=VALUE"]) == 2
    assert "--env is only supported" in capsys.readouterr().err
    assert main(["mcp", "add", "remote", "--url", "https://example.test/mcp", "--header", "broken"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err
