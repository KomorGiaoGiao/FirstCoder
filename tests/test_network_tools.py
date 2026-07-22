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


def test_fetch_rejects_private_and_local_targets(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    for url in (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
    ):
        result = registry.execute("fetch", {"url": url})
        assert result.ok is False, url
        assert result.error == "拒绝访问本机、内网或链路本地地址"


def _fake_web_search_response(body: str):
    """辅助：创建一个返回指定 text 的 fake urlopen。"""
    payload = web_search_module.dumps_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": body}]},
        }
    )

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

    return FakeContext()


def test_web_search_uses_parallel_by_default(monkeypatch, tmp_path):
    """默认使用 Parallel MCP（不是 Exa）。"""
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        return _fake_web_search_response("parallel results")

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(web_search_module.request, "urlopen", fake_urlopen)
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("web_search", {"query": "FirstCoder agent", "num_results": 3})

    assert result.ok is True
    assert "search.parallel.ai" in captured["url"]
    assert '"name":"web_search"' in captured["body"]
    assert '"num_results":3' in captured["body"]


def test_web_search_falls_back_to_exa_with_api_key(monkeypatch, tmp_path):
    """有 EXA_API_KEY 且 Parallel 失败时 fallback 到 Exa。"""
    captured = {}
    call_count = 0

    def fake_urlopen(req, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Parallel 先失败
            raise web_search_module.error.URLError("parallel failed")
        # Exa
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        return _fake_web_search_response("exa results")

    monkeypatch.setenv("EXA_API_KEY", "secret-token")
    monkeypatch.setattr(web_search_module.request, "urlopen", fake_urlopen)
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("web_search", {"query": "FirstCoder"})

    assert result.ok is True
    assert result.content == "exa results"
    assert "mcp.exa.ai" in captured["url"]
    assert '"name":"web_search_exa"' in captured["body"]


def test_web_search_redacts_exa_api_key_from_result_data(monkeypatch, tmp_path):
    """有 EXA key 时，强制 Exa provider 后 url 中的 key 会被脱敏。"""
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _fake_web_search_response("search results")

    monkeypatch.setenv("EXA_API_KEY", "secret-token")
    monkeypatch.setenv("FIRSTCODER_WEBSEARCH_PROVIDER", "exa")
    monkeypatch.setattr(web_search_module.request, "urlopen", fake_urlopen)
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("web_search", {"query": "FirstCoder"})

    assert "secret-token" in captured["url"]
    assert result.ok is True
    assert result.data.get("url", "") == "https://mcp.exa.ai/mcp?exaApiKey=%2A%2A%2A"


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


def test_web_search_definition_constrains_enum_parameters(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)
    definition = {item.name: item for item in registry.definitions()}["web_search"]
    properties = definition.parameters["properties"]

    assert properties["search_type"]["enum"] == ["auto", "fast", "deep"]
    assert properties["livecrawl"]["enum"] == ["fallback", "preferred"]


def test_web_search_normalizes_common_model_boolean_livecrawl(monkeypatch, tmp_path):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = req.data.decode("utf-8")
        return _fake_web_search_response("search results")

    monkeypatch.setattr(web_search_module.request, "urlopen", fake_urlopen)
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    result = registry.execute("web_search", {"query": "FirstCoder", "livecrawl": "false"})

    assert result.ok is True
    # Parallel 不支持 livecrawl，所以 body 里不该有 livecrawl
    assert "livecrawl" not in captured["body"]
