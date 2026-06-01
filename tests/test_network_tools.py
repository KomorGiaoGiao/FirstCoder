"""网络类工具行为测试。"""

from __future__ import annotations

from firstcoder.tools import fetch as fetch_module
from firstcoder.tools import web_search as web_search_module
from firstcoder.tools.fetch import create_fetch_tool
from firstcoder.tools.web_search import create_web_search_tool
from firstcoder.tools import create_builtin_registry


def test_fetch_reads_text_response(monkeypatch, tmp_path):
    class FakeResponse:
        status = 200

        def read(self):
            return "hello".encode("utf-8")

        def getheaders(self):
            return [("Content-Type", "text/plain")]

    class FakeContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(fetch_module.request, "urlopen", lambda request, timeout: FakeContext())
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("fetch", {"url": "https://example.com"})

    assert result.ok is True
    assert result.content == "hello"
    assert result.data["status"] == 200


def test_fetch_rejects_unsupported_scheme(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("fetch", {"url": "file:///etc/passwd"})

    assert result.ok is False
    assert result.error == "只支持 http 和 https URL"


def test_web_search_calls_exa_mcp(monkeypatch, tmp_path):
    payload = web_search_module.dumps_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "search results"}]},
        }
    )
    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return payload.encode("utf-8")

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        return FakeContext()

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(web_search_module.request, "urlopen", fake_urlopen)
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("web_search", {"query": "FirstCoder agent", "num_results": 3})

    assert result.ok is True
    assert result.content == "search results"
    assert captured["url"] == "https://mcp.exa.ai/mcp"
    assert '"name":"web_search_exa"' in captured["body"]
    assert '"numResults":3' in captured["body"]


def test_web_search_parses_sse_response():
    payload = web_search_module.dumps_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "sse results"}]},
        }
    )

    result = web_search_module.parse_mcp_search_response(f"event: message\ndata: {payload}\n\n")

    assert result == "sse results"


def test_web_search_rejects_invalid_limits(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    results = registry.execute("web_search", {"query": "x", "num_results": 0})
    chars = registry.execute("web_search", {"query": "x", "context_max_characters": 0})

    assert results.ok is False
    assert results.error == "num_results 必须大于 0"
    assert chars.ok is False
    assert chars.error == "context_max_characters 必须大于 0"
