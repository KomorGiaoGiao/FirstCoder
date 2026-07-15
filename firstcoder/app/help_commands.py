"""Help slash command."""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.app.commands import CommandResult


HELP_COMMANDS = [
    ("/new [title]", "Start a new session."),
    ("/fork [title]", "Copy the current session into a new branch."),
    ("/sessions", "List saved sessions."),
    ("/session <session_id>", "Show one session summary."),
    ("/resume", "Pick a session to resume."),
    ("/resume <session_id>", "Resume a session directly."),
    ("/share [session_id] [--tool-results]", "Export a shareable transcript."),
    ("/rename <title>", "Rename the current session."),
    ("/model", "Pick a model to use."),
    ("/model <model|provider/model>", "Switch the active model."),
    ("/skills", "Pick a skill to reference."),
    ("/skill <name>", "Show skill details."),
    ("/context", "Inspect context state."),
    ("/compact status", "Show compaction status."),
    ("/compact", "Compact context now."),
    ("/mode", "Show permission mode."),
    ("/mode <conservative|standard|aggressive|bypass>", "Change permission mode."),
    ("/mcp list", "List MCP server status."),
    ("/mcp doctor <server>", "Inspect one MCP server."),
]


HELP_TEXT = "\n".join(["Commands:", *[f"{command.ljust(48)} {description}" for command, description in HELP_COMMANDS]])


@dataclass(slots=True)
class HelpCommandHandler:
    """Render the current TUI slash command surface."""

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/help":
            return CommandResult(handled=False)
        return CommandResult(handled=True, output=HELP_TEXT)
