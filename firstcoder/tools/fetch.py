"""`fetch` 工具。"""

from __future__ import annotations

from urllib import error, parse, request

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.text import truncate


DEFAULT_FETCH_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CHARS = 20000


def create_fetch_tool() -> Tool:
    """创建 HTTP GET 工具。"""

    def fetch(url: str, timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS, max_chars: int = DEFAULT_MAX_CHARS) -> ToolResult:
        """读取 http 或 https URL 的文本响应。"""

        parsed = parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return make_error_result("fetch", "只支持 http 和 https URL")
        if timeout_seconds <= 0:
            return make_error_result("fetch", "timeout_seconds 必须大于 0")
        if max_chars <= 0:
            return make_error_result("fetch", "max_chars 必须大于 0")

        req = request.Request(url, headers={"User-Agent": "FirstCoder/0.1"})
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                body = response.read()
                status = getattr(response, "status", None)
                headers = dict(response.getheaders())
        except error.URLError as exc:
            return make_error_result("fetch", f"请求失败：{exc}")

        text = body.decode("utf-8", errors="replace")
        content, truncated = truncate(text, max_chars, suffix="\n\n[响应已截断]")

        return make_text_result(
            "fetch",
            content,
            url=url,
            status=status,
            headers=headers,
            truncated=truncated,
        )

    return tool_from_function(fetch)
