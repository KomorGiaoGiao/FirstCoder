"""source_code 输出的确定性压缩器。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart


_IMPORT_RE = re.compile(
    r"^\s*(import|from|package|use|#include)\b|^\s*(const|let|var)\s+\w+\s*=\s*require\(",
)
_SIGNATURE_RE = re.compile(
    r"^\s*(@\w+|async\s+def\s+\w+|def\s+\w+|class\s+\w+|function\s+\w+|"
    r"export\s+(async\s+)?function\s+\w+|export\s+class\s+\w+|"
    r"(public|private|protected)?\s*(static\s+)?\w+[\w<>,\s\[\]]+\s+\w+\s*\(|"
    r"func\s+(\([^)]+\)\s*)?\w+\s*\(|fn\s+\w+\s*\()",
)
_TYPE_RE = re.compile(r"^\s*(interface|type|struct|enum|trait)\s+\w+|^\s*export\s+(interface|type)\s+\w+")
_IMPORTANT_RE = re.compile(r"\b(TODO|FIXME|BUG|HACK|ERROR|WARN|WARNING|raise|throw|panic!)\b", re.IGNORECASE)


@dataclass(slots=True)
class SourceCodeRouteCompressor:
    max_body_lines_after_signature: int = 2
    max_total_lines: int = 120

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        lines = part.content.splitlines()
        if not lines or not _looks_like_source_code(lines):
            return None

        selected = self._select_indexes(lines)
        if not selected:
            return None

        sorted_selected = sorted(selected)
        output = [
            "[Source code compacted]",
            f"language={_detect_language(lines)}",
            f"original_lines={len(lines)}",
            f"kept_lines={len(sorted_selected)}",
            "",
        ]
        last_index: int | None = None
        omitted_ranges = 0
        for index in sorted_selected:
            if last_index is not None and index > last_index + 1:
                output.append(f"[... omitted {index - last_index - 1} lines]")
                omitted_ranges += 1
            output.append(lines[index])
            last_index = index

        omitted_lines = max(0, len(lines) - len(sorted_selected))
        if omitted_lines:
            output.append(f"[... omitted {omitted_lines} total lines]")

        return RouteCompactResult(
            content="\n".join(output),
            content_type=RouteContentType.SOURCE_CODE,
            compacted_by="l2_source_code",
            metadata={
                "code_language": _detect_language(lines),
                "code_original_lines": len(lines),
                "code_kept_lines": len(sorted_selected),
                "code_omitted_lines": omitted_lines,
                "code_omitted_ranges": omitted_ranges,
                "code_import_lines": _count_matching(lines, _IMPORT_RE),
                "code_signature_lines": _count_matching(lines, _SIGNATURE_RE),
                "code_type_lines": _count_matching(lines, _TYPE_RE),
            },
        )

    def _select_indexes(self, lines: list[str]) -> set[int]:
        selected: set[int] = set()
        for index, line in enumerate(lines):
            if _IMPORT_RE.search(line) or _TYPE_RE.search(line) or _IMPORTANT_RE.search(line):
                selected.add(index)
                continue
            if _SIGNATURE_RE.search(line):
                selected.add(index)
                _add_body_preview(
                    selected,
                    lines,
                    index,
                    max_body_lines=self.max_body_lines_after_signature,
                )

        if selected:
            selected.add(0)
            selected.add(len(lines) - 1)

        if len(selected) > self.max_total_lines:
            ranked = sorted(selected, key=lambda index: (_line_score(lines[index]), -index), reverse=True)
            return set(sorted(ranked[: self.max_total_lines]))
        return selected


def _looks_like_source_code(lines: list[str]) -> bool:
    sample = "\n".join(lines[:200])
    return bool(_IMPORT_RE.search(sample) or _SIGNATURE_RE.search(sample) or _TYPE_RE.search(sample))


def _detect_language(lines: list[str]) -> str:
    sample = "\n".join(lines[:200])
    if re.search(r"^\s*(def|class|import|from|async def)\b", sample, re.MULTILINE):
        return "python"
    if re.search(r"^\s*(interface|type)\s+\w+|:\s*(string|number|boolean)\b", sample, re.MULTILINE):
        return "typescript"
    if re.search(r"^\s*(function|const|let|var|export)\b", sample, re.MULTILINE):
        return "javascript"
    if re.search(r"^\s*(package|func)\b", sample, re.MULTILINE):
        return "go"
    if re.search(r"^\s*(use|fn|struct|impl|pub)\b", sample, re.MULTILINE):
        return "rust"
    if re.search(r"^\s*#include\b", sample, re.MULTILINE):
        return "c_or_cpp"
    return "unknown"


def _add_body_preview(
    selected: set[int],
    lines: list[str],
    signature_index: int,
    *,
    max_body_lines: int,
) -> None:
    kept = 0
    for index in range(signature_index + 1, min(len(lines), signature_index + 16)):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        if _SIGNATURE_RE.search(line) or _TYPE_RE.search(line):
            break
        selected.add(index)
        kept += 1
        if kept >= max_body_lines:
            break


def _count_matching(lines: list[str], pattern: re.Pattern[str]) -> int:
    return sum(1 for line in lines if pattern.search(line))


def _line_score(line: str) -> int:
    if _IMPORT_RE.search(line):
        return 100
    if _SIGNATURE_RE.search(line):
        return 90
    if _TYPE_RE.search(line):
        return 80
    if _IMPORTANT_RE.search(line):
        return 70
    return 10
