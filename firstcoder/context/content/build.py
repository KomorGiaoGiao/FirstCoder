"""build_output / shell log 输出的确定性压缩器。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart


_ERROR_RE = re.compile(
    r"(ERROR|FAILED|FAIL\b|FATAL|Traceback \(most recent call last\)|Exception|AssertionError|npm ERR!|panic|error:)",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(r"\b(WARN|WARNING|warning:)\b", re.IGNORECASE)
_SUMMARY_RE = re.compile(
    r"(^=+|^-+|\b\d+\s+(passed|failed|skipped|error|errors|warning|warnings)\b|"
    r"\b(Build|Compile|Test|Tests|Suites?).*(succeeded|failed|complete|passed)\b)",
    re.IGNORECASE,
)
_STACK_RE = re.compile(
    r"(^\s*File \".+\", line \d+|^\s*at .+\(.+:\d+:\d+\)|^\s+at [\w.$]+|^\s*-->\s+.+:\d+:\d+)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class BuildOutputRouteCompressor:
    max_error_blocks: int = 8
    context_lines: int = 2
    max_warnings: int = 6
    max_summary_lines: int = 12
    max_total_lines: int = 120

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        lines = part.content.splitlines()
        if not lines or not _looks_like_build_output(lines):
            return None

        selected_indexes = self._select_indexes(lines)
        if not selected_indexes:
            return None

        selected = sorted(selected_indexes)
        output: list[str] = ["[Build output compacted]"]
        last_index: int | None = None
        omitted_ranges = 0
        for index in selected:
            if last_index is not None and index > last_index + 1:
                output.append(f"[... omitted {index - last_index - 1} lines]")
                omitted_ranges += 1
            output.append(lines[index])
            last_index = index

        omitted_lines = max(0, len(lines) - len(selected))
        if omitted_lines:
            output.append(f"[... omitted {omitted_lines} total lines]")

        return RouteCompactResult(
            content="\n".join(output),
            content_type=RouteContentType.BUILD_OUTPUT,
            compacted_by="l2_build_output",
            metadata={
                "build_original_lines": len(lines),
                "build_kept_lines": len(selected),
                "build_omitted_lines": omitted_lines,
                "build_omitted_ranges": omitted_ranges,
                "build_error_lines": _count_matching(lines, _ERROR_RE),
                "build_warning_lines": _count_matching(lines, _WARNING_RE),
                "build_summary_lines": _count_matching(lines, _SUMMARY_RE),
            },
        )

    def _select_indexes(self, lines: list[str]) -> set[int]:
        selected: set[int] = set()
        error_indexes = [index for index, line in enumerate(lines) if _ERROR_RE.search(line)]
        warning_indexes = [index for index, line in enumerate(lines) if _WARNING_RE.search(line)]
        summary_indexes = [index for index, line in enumerate(lines) if _SUMMARY_RE.search(line)]
        stack_indexes = [index for index, line in enumerate(lines) if _STACK_RE.search(line)]

        for index in _first_last(error_indexes, self.max_error_blocks):
            _add_context(selected, index, line_count=len(lines), radius=self.context_lines)
            _add_following_stack(selected, lines, index)

        for index in warning_indexes[: self.max_warnings]:
            _add_context(selected, index, line_count=len(lines), radius=1)

        for index in summary_indexes[: self.max_summary_lines]:
            selected.add(index)

        for index in stack_indexes:
            selected.add(index)

        if selected:
            selected.add(0)
            selected.add(len(lines) - 1)

        if len(selected) > self.max_total_lines:
            priority = _rank_indexes(lines, selected)
            return set(sorted(priority[: self.max_total_lines]))
        return selected


def _looks_like_build_output(lines: list[str]) -> bool:
    joined = "\n".join(lines)
    return bool(_ERROR_RE.search(joined) or _WARNING_RE.search(joined) or _SUMMARY_RE.search(joined))


def _count_matching(lines: list[str], pattern: re.Pattern[str]) -> int:
    return sum(1 for line in lines if pattern.search(line))


def _first_last(indexes: list[int], max_count: int) -> list[int]:
    if len(indexes) <= max_count:
        return list(indexes)
    selected = [indexes[0]]
    if max_count > 1:
        selected.append(indexes[-1])
    for index in indexes[1:-1]:
        if len(selected) >= max_count:
            break
        selected.append(index)
    return sorted(set(selected))


def _add_context(selected: set[int], index: int, *, line_count: int, radius: int) -> None:
    for item in range(max(0, index - radius), min(line_count, index + radius + 1)):
        selected.add(item)


def _add_following_stack(selected: set[int], lines: list[str], index: int) -> None:
    for next_index in range(index + 1, min(len(lines), index + 12)):
        line = lines[next_index]
        if _STACK_RE.search(line) or line.startswith((" ", "\t")):
            selected.add(next_index)
            continue
        break


def _rank_indexes(lines: list[str], indexes: set[int]) -> list[int]:
    return sorted(indexes, key=lambda index: (_line_score(lines[index]), -index), reverse=True)


def _line_score(line: str) -> int:
    if _ERROR_RE.search(line):
        return 100
    if _STACK_RE.search(line):
        return 90
    if _SUMMARY_RE.search(line):
        return 70
    if _WARNING_RE.search(line):
        return 50
    return 10
