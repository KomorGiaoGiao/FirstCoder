from firstcoder.mcp.search import (
    MCP_TOOL_SEARCH_LIMIT,
    McpSearchEntry,
    create_mcp_tool_search,
    search_mcp_tools,
)
from firstcoder.providers.types import ToolDefinition


def _entry(server: str, tool: str, description: str) -> McpSearchEntry:
    return McpSearchEntry(
        server=server,
        tool=tool,
        definition=ToolDefinition(
            name=f"mcp__{server}__{tool}",
            description=description,
            parameters={"type": "object", "properties": {}},
        ),
    )


def test_search_mcp_tools_prefers_exact_name_then_name_server_and_description() -> None:
    entries = (
        _entry("github", "get_pull_request", "Read one pull request."),
        _entry("github", "search_pull_requests", "Search pull requests."),
        _entry("tracker", "lookup", "Get a GitHub pull request."),
    )

    matches = search_mcp_tools(entries, "get pull request")

    assert [item.tool for item in matches] == [
        "get_pull_request",
        "search_pull_requests",
        "lookup",
    ]


def test_search_mcp_tools_is_stable_and_limited() -> None:
    entries = tuple(
        _entry("demo", f"lookup_{index:02d}", "Lookup records.")
        for index in reversed(range(20))
    )

    matches = search_mcp_tools(entries, "lookup")

    assert len(matches) == MCP_TOOL_SEARCH_LIMIT == 8
    assert [item.definition.name for item in matches] == sorted(
        item.definition.name for item in matches
    )


def test_mcp_tool_search_returns_activated_names_without_executing_mcp() -> None:
    entries = (_entry("github", "get_issue", "Read one issue."),)
    tool = create_mcp_tool_search(entries)

    result = tool.executor(query="read github issue")

    assert result.ok is True
    assert result.data["mcp_tool_search"]["activated_tools"] == [
        "mcp__github__get_issue"
    ]
    assert "mcp__github__get_issue" in result.content
    assert tool.permission is None
    assert tool.definition.parameters["required"] == ["query"]
    assert tool.definition.parameters["additionalProperties"] is False


def test_mcp_tool_search_rejects_blank_query_and_handles_no_match() -> None:
    tool = create_mcp_tool_search(
        (_entry("github", "get_issue", "Read one issue."),)
    )

    blank = tool.executor(query="   ")
    missing = tool.executor(query="calendar event")

    assert blank.ok is False
    assert blank.data["mcp_tool_search"]["activated_tools"] == []
    assert missing.ok is True
    assert missing.data["mcp_tool_search"]["activated_tools"] == []
