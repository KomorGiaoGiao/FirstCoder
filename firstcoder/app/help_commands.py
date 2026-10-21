"""/help 静态帮助文本。"""

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
    ("/mode <standard|aggressive|bypass>", "Change permission mode."),
    ("/mcp list", "List MCP server status."),
    ("/mcp doctor <server>", "Inspect one MCP server."),
    ("/mcp reconnect <server|all>", "Reconnect MCP servers in the background."),
]


HELP_TEXT = "\n".join(["Commands:", *[f"{command.ljust(48)} {description}" for command, description in HELP_COMMANDS]])


@dataclass(slots=True)
class HelpCommandHandler:
    """Render the current TUI slash command surface."""

    def handle(self, text: str) -> CommandResult:
        """精确匹配 /help 返回静态帮助文本。

        空白归一化后比较, 多余空格不影响匹配; 其他输入返回 handled=False 让位
        给后续 handler。帮助文本是静态表, 新增命令时需同步维护 HELP_COMMANDS。
        """
        command = " ".join(text.strip().split())
        if command != "/help":
            return CommandResult(handled=False)
        return CommandResult(handled=True, output=HELP_TEXT)
