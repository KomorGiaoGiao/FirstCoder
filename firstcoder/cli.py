"""Command-line entry point for single-turn FirstCoder runs."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.app.factory import create_firstcoder_app
from firstcoder.config import load_config
from firstcoder.config.settings import default_global_config_path, project_config_path, render_default_config


@dataclass(frozen=True, slots=True)
class CliConfig:
    project_root: Path
    data_root: Path | None
    session_id: str | None
    provider_name: str | None
    message: str
    max_tool_rounds: int | None = None


CliRunner = Callable[[CliConfig], str]


class ChatRunnerLike(Protocol):
    last_pending_input: object | None

    def run_user_turn(self, content: str):
        ...

    def resume_with_user_input(self, request_id: str, answer: str):
        ...


def read_message(message: str | None, *, stdin_text: str | None = None) -> str:
    """Return a user message from an argument or stdin."""

    if message is not None:
        return message.strip()
    text = sys.stdin.read() if stdin_text is None else stdin_text
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a single FirstCoder user turn.")
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Inspect or initialize FirstCoder configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("path", help="Show global and project config paths.")
    config_subparsers.add_parser("show", help="Show effective provider configuration without secrets.")
    init_parser = config_subparsers.add_parser("init", help="Create a starter global config file.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite the existing global config.")

    parser.add_argument("--project", default=".", help="Project root for tools and AGENTS.md.")
    parser.add_argument("--data-root", default=None, help="Directory for FirstCoder session data.")
    parser.add_argument("--session-id", default=None, help="Session id to create or reuse.")
    parser.add_argument("--provider", default=None, help="Provider name override.")
    parser.add_argument("--message", default=None, help="Single user message. Reads stdin when omitted.")
    parser.add_argument("--interactive", action="store_true", help="Run a line-oriented interactive session.")
    parser.add_argument("--tui", action="store_true", help="Run the Textual TUI.")
    parser.add_argument("--auto-approve", action="store_true", help="Automatically answer permission confirmations with allow_once.")
    parser.add_argument("--max-tool-rounds", type=_positive_int, default=None, help="Override per-turn tool round limit.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner: CliRunner | None = None,
    stdin_text: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "config":
        return run_config_command(args)

    if args.tui or (args.message is None and stdin_text is None and sys.stdin.isatty() and not args.interactive):
        config = CliConfig(
            project_root=Path(args.project),
            data_root=Path(args.data_root) if args.data_root is not None else None,
            session_id=args.session_id,
            provider_name=args.provider,
            message="",
            max_tool_rounds=args.max_tool_rounds,
        )
        try:
            app = create_cli_app(config)
            app.run()
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.interactive:
        config = CliConfig(
            project_root=Path(args.project),
            data_root=Path(args.data_root) if args.data_root is not None else None,
            session_id=args.session_id,
            provider_name=args.provider,
            message="",
            max_tool_rounds=args.max_tool_rounds,
        )
        try:
            app = create_cli_app(config)
            lines = stdin_text.splitlines() if stdin_text is not None else None
            run_repl(app.chat_runner, lines, auto_approve=args.auto_approve)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    message = read_message(args.message, stdin_text=stdin_text)
    if not message:
        print("error: message is required via --message or stdin", file=sys.stderr)
        return 2

    config = CliConfig(
        project_root=Path(args.project),
        data_root=Path(args.data_root) if args.data_root is not None else None,
        session_id=args.session_id,
        provider_name=args.provider,
        message=message,
        max_tool_rounds=args.max_tool_rounds,
    )
    run = runner or run_single_turn
    try:
        output = run(config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


def run_single_turn(config: CliConfig) -> str:
    app = create_cli_app(config)
    response = app.chat_runner.run_user_turn(config.message)
    return response.content


def create_cli_app(config: CliConfig):
    provider = None
    if config.provider_name is not None:
        from firstcoder.providers.factory import create_provider

        provider = create_provider(config.provider_name, project_root=config.project_root)
    app = create_firstcoder_app(
        project_root=config.project_root,
        data_root=config.data_root,
        provider=provider,
        session_id=config.session_id,
    )
    if config.max_tool_rounds is not None:
        app.chat_runner.limits = AgentLoopLimits.default().with_max_tool_rounds(config.max_tool_rounds)
    return app


def run_config_command(args: argparse.Namespace) -> int:
    command = args.config_command or "show"
    project_root = Path(args.project)
    if command == "path":
        print(f"global: {default_global_config_path()}")
        print(f"project: {project_config_path(project_root)}")
        return 0
    if command == "init":
        path = default_global_config_path()
        if path.exists() and not args.force:
            print(f"config already exists: {path}", file=sys.stderr)
            print("use --force to overwrite", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_default_config(), encoding="utf-8")
        print(f"created: {path}")
        return 0
    if command == "show":
        config = load_config(args.provider, project_root=project_root)
        print(f"provider: {config.provider_name}")
        print(f"model: {_effective_model(config)}")
        print(f"base_url: {_effective_base_url(config)}")
        print("config_files:")
        for path in config.loaded_config_paths:
            print(f"  - {path}")
        if not config.loaded_config_paths:
            print("  - <none>")
        return 0
    print(f"error: unknown config command: {command}", file=sys.stderr)
    return 2


def _effective_model(config) -> str:
    model = config.get_config_value("model") or config.get_env("FIRSTCODER_MODEL")
    return model or "<provider default>"


def _effective_base_url(config) -> str:
    base_url = config.get_provider_value("base_url", env="FIRSTCODER_BASE_URL")
    return base_url or "<provider default>"


def run_repl(
    chat_runner: ChatRunnerLike,
    lines: Iterable[str] | None = None,
    *,
    auto_approve: bool = False,
) -> None:
    source = iter(lines) if lines is not None else _stdin_lines()
    pending = None
    for raw_line in source:
        line = raw_line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break

        if pending is not None:
            if _pending_kind(pending) == "permission_confirmation":
                choice = _permission_choice_for_text(line, pending)
                if choice is None:
                    print(f"Unknown permission choice: {line}")
                    print(_permission_choice_help_text(pending))
                    print(_permission_options_text(pending))
                    continue
                line = choice
            response = chat_runner.resume_with_user_input(_pending_id(pending), line)
        else:
            response = chat_runner.run_user_turn(line)

        print(f"FirstCoder> {response.content}")
        pending = getattr(chat_runner, "last_pending_input", None)
        while pending is not None and auto_approve and _pending_kind(pending) == "permission_confirmation":
            print("Auto-approve> allow_once")
            response = chat_runner.resume_with_user_input(_pending_id(pending), "allow_once")
            print(f"FirstCoder> {response.content}")
            pending = getattr(chat_runner, "last_pending_input", None)

        if pending is not None:
            if _pending_kind(pending) == "permission_confirmation":
                print(_permission_options_text(pending))
            else:
                print(f"Permission> {_pending_question(pending)}")


def _stdin_lines():
    prompt = _create_prompt_session()
    if prompt is not None:
        while True:
            try:
                yield prompt.prompt("You> ")
            except (EOFError, KeyboardInterrupt):
                break
        return

    while True:
        try:
            yield input("You> ")
        except EOFError:
            break


def _create_prompt_session():
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
    except ImportError:
        return None
    return PromptSession(history=InMemoryHistory())


def _pending_id(pending: object) -> str:
    return str(getattr(pending, "id"))


def _pending_question(pending: object) -> str:
    return str(getattr(pending, "question", "需要用户输入。"))


def _pending_kind(pending: object) -> str:
    return str(getattr(pending, "kind", ""))


def _permission_choice_for_text(text: str, pending: object) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    aliases = {
        "1": "deny",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "2": "allow_once",
        "y": "allow_once",
        "yes": "allow_once",
        "allow": "allow_once",
        "once": "allow_once",
        "allow_once": "allow_once",
        "3": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    if normalized in aliases:
        return aliases[normalized]

    for index, option in enumerate(_permission_options(pending), start=1):
        option_id = _option_id(option)
        label = _option_label(option)
        values = {
            str(index).lower(),
            option_id.lower(),
            label.strip().lower().replace(" ", "_"),
        }
        if normalized in values:
            return option_id
    return None


def _permission_options_text(pending: object) -> str:
    question = _pending_question(pending)
    options = _permission_options(pending)
    option_lines = [
        f"  {index}. {_option_label(option)}"
        + (f" ({_option_id(option)})" if _option_id(option) != _option_label(option) else "")
        for index, option in enumerate(options, start=1)
    ]
    if not option_lines:
        option_lines = [
            "  1. Deny",
            "  2. Allow once",
            "  3. Allow always for same scope",
        ]
    return "\n".join(
        [
            f"Permission> {question}",
            "Choose:",
            *option_lines,
        ]
    )


def _permission_choice_help_text(pending: object) -> str:
    count = len(_permission_options(pending)) or 3
    choices = ", ".join(str(index) for index in range(1, count + 1))
    return f"Please choose {choices}."


def _permission_options(pending: object) -> list[object]:
    return list(getattr(pending, "options", []) or [])


def _option_id(option: object) -> str:
    if isinstance(option, dict):
        return str(option.get("id") or option.get("label") or "")
    return str(getattr(option, "id", getattr(option, "label", "")))


def _option_label(option: object) -> str:
    if isinstance(option, dict):
        return str(option.get("label") or option.get("id") or "")
    return str(getattr(option, "label", getattr(option, "id", "")))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
