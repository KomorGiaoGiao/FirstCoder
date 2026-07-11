from firstcoder.context.content.router import (
    RouteCompactResult,
    RouteCompressor,
    RouteContentType,
    RouteContext,
    RouteCompactRouter,
    detect_route_content_type,
)
from firstcoder.context.content.build import BuildOutputRouteCompressor
from firstcoder.context.content.code import SourceCodeRouteCompressor
from firstcoder.context.content.compressors import PlainTextRouteCompressor
from firstcoder.context.content.diff import GitDiffRouteCompressor
from firstcoder.context.content.html import HtmlRouteCompressor
from firstcoder.context.content.json import JsonRouteCompressor
from firstcoder.context.content.search import SearchResultsRouteCompressor
from firstcoder.context.models import MessagePart


class StaticCompressor:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[MessagePart, RouteContext]] = []

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult:
        self.calls.append((part, context))
        return RouteCompactResult(
            content=self.content,
            content_type=RouteContentType.SEARCH_RESULTS,
            compacted_by="test_static",
            metadata={"example": "yes"},
        )


def _part(content: str, *, tool_name: str = "grep") -> MessagePart:
    return MessagePart(
        id="part_1",
        message_id="msg_1",
        kind="text",
        content=content,
        metadata={"tool_name": tool_name},
    )


def test_route_compact_dispatches_by_content_type() -> None:
    compressor = StaticCompressor("[search results compacted]\nmatch_count=2")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )
    part = _part("firstcoder/app.py:10:def run():\nfirstcoder/app.py:20:def stop():")

    result = router.compact_part(part)

    assert result is not None
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["compacted_by"] == "test_static"
    assert compressor.calls[0][0] is part


def test_route_compact_rejects_output_that_is_not_smaller() -> None:
    original = "firstcoder/app.py:10:def run():"
    compressor = StaticCompressor(original + "\nextra metadata that makes output larger")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(original))

    assert result is None


def test_route_compact_adds_metadata() -> None:
    compressor = StaticCompressor("[search results compacted]")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )

    result = router.compact_part(_part("firstcoder/app.py:10:def run():\n" * 20))

    assert result is not None
    assert result.id == "part_1"
    assert result.message_id == "msg_1"
    assert result.kind == "text"
    assert result.metadata["compaction_state"] == "route_compacted"
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["route_confidence"] > 0
    assert result.metadata["original_tokens"] > result.metadata["replacement_tokens"]
    assert result.metadata["content_fingerprint"]
    assert result.metadata["compaction_strategy_version"] == "v2"


def test_route_compact_skips_when_no_compressor_is_registered() -> None:
    router = RouteCompactRouter(compressors={}, min_original_tokens=1)

    result = router.compact_part(_part("firstcoder/app.py:10:def run():"))

    assert result is None


def test_route_compact_falls_back_to_plain_text_for_unregistered_detected_type() -> None:
    content = '{"error":"x",' + ",".join(f'"key_{line}":"value_{line}"' for line in range(1, 120)) + "}"
    router = RouteCompactRouter(
        compressors={RouteContentType.PLAIN_TEXT: PlainTextRouteCompressor()},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="shell"))

    assert result is not None
    assert result.metadata["content_type"] == "plain_text"
    assert result.metadata["detected_content_type"] == "json_object"
    assert result.metadata["route_fallback_from"] == "json_object"


def test_detector_prefers_json_over_shell_build_hint() -> None:
    detection = detect_route_content_type(
        '[{"status":"failed","error":"TimeoutError"}]',
        tool_name="shell",
    )

    assert detection.content_type == RouteContentType.JSON_ARRAY


def test_detector_prefers_source_code_over_build_keywords() -> None:
    detection = detect_route_content_type(
        "def run():\n    raise RuntimeError('failed')\n",
        tool_name="shell",
    )

    assert detection.content_type == RouteContentType.SOURCE_CODE


def test_search_results_compressor_groups_files_and_records_omissions() -> None:
    content = "\n".join(
        [
            "firstcoder/app.py:1: def old_1(): pass",
            *[f"firstcoder/app.py:{line}: def old_{line}(): pass with repeated context" for line in range(2, 40)],
            "firstcoder/app.py:3: TODO important path",
            "firstcoder/tools.py:10: normal match",
            *[f"firstcoder/tools.py:{line}: normal match with repeated context" for line in range(12, 50)],
            "firstcoder/tools.py:11: ERROR must keep this",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(max_matches_per_file=3)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content))

    assert result is not None
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["compacted_by"] == "l2_search_results"
    assert result.metadata["search_original_matches"] > 70
    assert result.metadata["search_kept_matches"] < result.metadata["search_original_matches"]
    assert "firstcoder/app.py:1:" in result.content
    assert "firstcoder/app.py:3: TODO important path" in result.content
    assert "firstcoder/tools.py:11: ERROR must keep this" in result.content
    assert "omitted" in result.content


def test_search_results_compressor_parses_windows_paths() -> None:
    content = "\n".join(
        rf"C:\repo\firstcoder\app.py:{line}: def run_{line}(): pass with repeated context"
        for line in range(1, 30)
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(max_matches_per_file=2)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content))

    assert result is not None
    assert result.metadata["search_file_count"] == 1
    assert r"C:\repo\firstcoder\app.py:1:" in result.content


def test_search_results_compressor_keeps_sentinel_like_matches() -> None:
    content = "\n".join(
        [
            *[f"firstcoder/context/manager.py:{line}: normal match {line}" for line in range(1, 180)],
            "firstcoder/context/manager.py:180: SEARCH_SENTINEL_9 important routing evidence",
            *[f"firstcoder/context/manager.py:{line}: trailing match {line}" for line in range(181, 360)],
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(max_matches_per_file=5)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="rg"))

    assert result is not None
    assert "SEARCH_SENTINEL_9 important routing evidence" in result.content


def test_git_diff_compressor_keeps_headers_changes_and_limited_context() -> None:
    content = "\n".join(
        [
            "diff --git a/firstcoder/app.py b/firstcoder/app.py",
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,25 +1,25 @@",
            *[f" context line {line}" for line in range(1, 20)],
            "-old behavior",
            "+new behavior",
            "+TODO keep important change",
            *[f" more context {line}" for line in range(20, 50)],
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.GIT_DIFF: GitDiffRouteCompressor(max_context_lines=1)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="git_diff"))

    assert result is not None
    assert result.metadata["content_type"] == "git_diff"
    assert result.metadata["compacted_by"] == "l2_git_diff"
    assert result.metadata["diff_files_affected"] == 1
    assert result.metadata["diff_additions"] == 2
    assert result.metadata["diff_deletions"] == 1
    assert result.metadata["diff_context_lines_omitted"] > 0
    assert "diff --git a/firstcoder/app.py b/firstcoder/app.py" in result.content
    assert "-old behavior" in result.content
    assert "+TODO keep important change" in result.content
    assert "omitted" in result.content


def test_git_diff_compressor_counts_unified_diff_without_git_header() -> None:
    content = "\n".join(
        [
            "--- a/firstcoder/app.py",
            "+++ b/firstcoder/app.py",
            "@@ -1,25 +1,25 @@",
            *[f" context line {line}" for line in range(1, 30)],
            "-old behavior",
            "+new behavior",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.GIT_DIFF: GitDiffRouteCompressor(max_context_lines=1)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="diff"))

    assert result is not None
    assert result.metadata["diff_files_affected"] == 1
    assert result.metadata["diff_hidden_files"] == 0
    assert "--- a/firstcoder/app.py" in result.content
    assert "+++ b/firstcoder/app.py" in result.content


def test_build_output_compressor_keeps_errors_tracebacks_and_summary() -> None:
    content = "\n".join(
        [
            "running pytest tests",
            *[f"collecting module_{line}" for line in range(1, 60)],
            "tests/test_app.py::test_run FAILED",
            "Traceback (most recent call last):",
            '  File "tests/test_app.py", line 12, in test_run',
            "    assert run() == 1",
            "AssertionError: expected 1",
            *[f"debug retry noise {line}" for line in range(60, 120)],
            "WARNING: slow test detected",
            "==================== short test summary info ====================",
            "FAILED tests/test_app.py::test_run - AssertionError",
            "1 failed, 4 passed in 0.42s",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.BUILD_OUTPUT: BuildOutputRouteCompressor(context_lines=1)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="pytest"))

    assert result is not None
    assert result.metadata["content_type"] == "build_output"
    assert result.metadata["compacted_by"] == "l2_build_output"
    assert result.metadata["build_error_lines"] >= 2
    assert result.metadata["build_warning_lines"] == 1
    assert result.metadata["build_omitted_lines"] > 0
    assert "tests/test_app.py::test_run FAILED" in result.content
    assert 'File "tests/test_app.py", line 12' in result.content
    assert "1 failed, 4 passed" in result.content


def test_build_output_compressor_detects_late_errors_in_large_logs() -> None:
    content = "\n".join(
        [
            "pytest tests/test_context.py -q",
            *[f"normal log line {line}" for line in range(1, 520)],
            "tests/test_context.py::test_resume FAILED",
            "Traceback (most recent call last):",
            '  File "tests/test_context.py", line 33, in test_resume',
            "    assert resume()",
            "AssertionError: RESUME_SENTINEL_42",
            *[f"more build noise {line}" for line in range(520, 1040)],
            "FAILED tests/test_context.py::test_resume - AssertionError: RESUME_SENTINEL_42",
            "1 failed, 120 passed in 9.99s",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.BUILD_OUTPUT: BuildOutputRouteCompressor(context_lines=1)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="pytest"))

    assert result is not None
    assert result.metadata["content_type"] == "build_output"
    assert result.metadata["compacted_by"] == "l2_build_output"
    assert result.metadata["build_omitted_lines"] > 900
    assert "AssertionError: RESUME_SENTINEL_42" in result.content
    assert "1 failed, 120 passed" in result.content


def test_json_array_compressor_keeps_anchors_and_important_items() -> None:
    items = [
        {"id": index, "status": "ok", "message": f"normal item {index}", "payload": "x" * 80}
        for index in range(40)
    ]
    items[25] = {
        "id": 25,
        "status": "failed",
        "error": "TimeoutError",
        "traceback": "Traceback line\n" * 20,
    }
    router = RouteCompactRouter(
        compressors={RouteContentType.JSON_ARRAY: JsonRouteCompressor(max_array_items=8)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(__import__("json").dumps(items), tool_name="shell"))

    assert result is not None
    assert result.metadata["content_type"] == "json_array"
    assert result.metadata["compacted_by"] == "l2_json_array"
    assert result.metadata["json_original_items"] == 40
    assert result.metadata["json_kept_items"] <= 8
    assert '"error":"TimeoutError"' in result.content
    assert '"omitted_items"' in result.content


def test_json_object_compressor_keeps_important_keys() -> None:
    obj = {f"field_{index}": "x" * 80 for index in range(50)}
    obj["error"] = "permission denied"
    obj["summary"] = "command failed after retry"
    router = RouteCompactRouter(
        compressors={RouteContentType.JSON_OBJECT: JsonRouteCompressor(max_object_keys=6)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(__import__("json").dumps(obj), tool_name="shell"))

    assert result is not None
    assert result.metadata["content_type"] == "json_object"
    assert result.metadata["compacted_by"] == "l2_json_object"
    assert result.metadata["json_original_keys"] == 52
    assert result.metadata["json_kept_keys"] <= 6
    assert '"error":"permission denied"' in result.content
    assert '"summary":"command failed after retry"' in result.content


def test_source_code_compressor_keeps_imports_signatures_and_important_lines() -> None:
    content = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "",
            "class Runner:",
            "    def run(self) -> None:",
            "        setup = 1",
            "        value = 2",
            *[f"        noise_{line} = {line}" for line in range(1, 80)],
            "        # TODO preserve this failure path",
            "        raise RuntimeError('failed')",
            "",
            "def helper(value: int) -> int:",
            "    return value + 1",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SOURCE_CODE: SourceCodeRouteCompressor(max_body_lines_after_signature=1)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="shell"))

    assert result is not None
    assert result.metadata["content_type"] == "source_code"
    assert result.metadata["compacted_by"] == "l2_source_code"
    assert result.metadata["code_language"] == "python"
    assert result.metadata["code_signature_lines"] >= 3
    assert result.metadata["code_omitted_lines"] > 0
    assert "import os" in result.content
    assert "class Runner:" in result.content
    assert "def helper(value: int) -> int:" in result.content
    assert "TODO preserve this failure path" in result.content


def test_html_compressor_extracts_visible_content_and_links() -> None:
    content = """
    <!doctype html>
    <html>
      <head>
        <title>FirstCoder Notes</title>
        <style>.hidden { display: none; }</style>
        <script>console.log("noise")</script>
      </head>
      <body>
        <nav>Navigation noise</nav>
        <main>
          <h1>Context Compression</h1>
          <h2>Route Compact</h2>
          <p>The route layer keeps useful visible content.</p>
          <a href="/docs/context">Read docs</a>
        </main>
    """
    content += "".join(f"<p>repeated paragraph {line}</p>" for line in range(1, 80))
    content += "</body></html>"
    router = RouteCompactRouter(
        compressors={RouteContentType.HTML: HtmlRouteCompressor(max_text_blocks=6)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="shell"))

    assert result is not None
    assert result.metadata["content_type"] == "html"
    assert result.metadata["compacted_by"] == "l2_html"
    assert result.metadata["html_title"] == "FirstCoder Notes"
    assert result.metadata["html_omitted_text_blocks"] > 0
    assert "Context Compression" in result.content
    assert "Read docs -> /docs/context" in result.content
    assert "console.log" not in result.content


def test_html_compressor_keeps_sentinel_like_visible_text_from_late_blocks() -> None:
    content = "<html><body>" + "".join(f"<p>paragraph {line}</p>" for line in range(1, 120))
    content += "<section>HTML_SENTINEL_88 important final state</section></body></html>"
    router = RouteCompactRouter(
        compressors={RouteContentType.HTML: HtmlRouteCompressor(max_text_blocks=8)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content, tool_name="shell"))

    assert result is not None
    assert "HTML_SENTINEL_88 important final state" in result.content


def test_plain_text_compressor_keeps_tail_when_preview_omits_it() -> None:
    content = ("普通说明段落，围绕压缩策略和 resume session 反复展开。" * 900) + " PLAIN_SENTINEL_END"
    router = RouteCompactRouter(
        compressors={RouteContentType.PLAIN_TEXT: PlainTextRouteCompressor()},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content))

    assert result is not None
    assert "PLAIN_SENTINEL_END" in result.content
