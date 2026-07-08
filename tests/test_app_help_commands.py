from firstcoder.app.help_commands import HelpCommandHandler


def test_help_command_lists_current_slash_commands() -> None:
    result = HelpCommandHandler().handle("/help")

    assert result.handled is True
    assert "Commands:" in result.output
    expected = {
        "/new [title]": "Start a new session.",
        "/fork [title]": "Copy the current session into a new branch.",
        "/sessions": "List saved sessions.",
        "/session <session_id>": "Show one session summary.",
        "/resume": "Pick a session to resume.",
        "/resume <session_id>": "Resume a session directly.",
        "/share [session_id] [--tool-results]": "Export a shareable transcript.",
        "/rename <title>": "Rename the current session.",
        "/skills": "List available skills.",
        "/skill <name>": "Show skill details.",
        "/context": "Inspect context state.",
        "/compact status": "Show compaction status.",
        "/compact": "Compact context now.",
        "/mode": "Show permission mode.",
        "/mode <conservative|standard|aggressive|bypass>": "Change permission mode.",
    }
    for command, description in expected.items():
        assert f"{command.ljust(48)} {description}" in result.output


def test_help_command_ignores_non_help_input() -> None:
    result = HelpCommandHandler().handle("/sessions")

    assert result.handled is False
