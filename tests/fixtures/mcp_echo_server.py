"""用于 MCP 集成测试的最小 stdio echo 服务端。"""

from mcp.server.fastmcp import FastMCP


server = FastMCP("firstcoder-integration-echo")


@server.tool()
def echo(message: str) -> str:
    """原样返回调用参数。"""

    return message


if __name__ == "__main__":
    server.run(transport="stdio")
