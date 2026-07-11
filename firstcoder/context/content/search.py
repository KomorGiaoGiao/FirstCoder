"""search_results / grep 输出的确定性压缩器。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart


@dataclass(slots=True)
class _SearchMatch:
    file_path: str
    line_number: int
    text: str
    original: str


@dataclass(slots=True)
class SearchResultsRouteCompressor:
    max_matches_per_file: int = 5
    max_files: int = 15

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        matches = _parse_search_matches(part.content)
        if not matches:
            return None

        grouped: dict[str, list[_SearchMatch]] = {}
        for match in matches:
            grouped.setdefault(match.file_path, []).append(match)

        lines: list[str] = [
            "[Search results compacted]",
            f"original_matches={len(matches)}",
            f"files={len(grouped)}",
        ]
        kept_count = 0
        omitted_total = 0

        for file_path in sorted(grouped.keys())[: self.max_files]:
            file_matches = sorted(grouped[file_path], key=lambda item: item.line_number)
            selected = _select_search_matches(file_matches, max_matches=self.max_matches_per_file)
            kept_count += len(selected)
            omitted = max(0, len(file_matches) - len(selected))
            omitted_total += omitted

            lines.append(f"\n## {file_path} ({len(file_matches)} matches)")
            for match in selected:
                lines.append(f"{match.file_path}:{match.line_number}: {match.text}")
            if omitted:
                lines.append(f"[... omitted {omitted} matches in {file_path}]")

        hidden_files = max(0, len(grouped) - self.max_files)
        if hidden_files:
            lines.append(f"\n[... omitted {hidden_files} files]")

        return RouteCompactResult(
            content="\n".join(lines),
            content_type=RouteContentType.SEARCH_RESULTS,
            compacted_by="l2_search_results",
            metadata={
                "search_original_matches": len(matches),
                "search_kept_matches": kept_count,
                "search_omitted_matches": omitted_total,
                "search_file_count": len(grouped),
                "search_hidden_files": hidden_files,
            },
        )


def _parse_search_matches(content: str) -> list[_SearchMatch]:
    matches: list[_SearchMatch] = []
    for line in content.splitlines():
        parsed = _split_search_line(line)
        if parsed is None:
            continue
        file_path, line_number, text = parsed
        matches.append(_SearchMatch(file_path=file_path, line_number=line_number, text=text, original=line))
    return matches


def _split_search_line(line: str) -> tuple[str, int, str] | None:
    first = line.find(":")
    if first == -1:
        return None

    search_from = 3 if first == 1 and len(line) > 2 and line[2] in ("\\", "/") else 0
    second = line.find(":", search_from)
    while second != -1:
        third = line.find(":", second + 1)
        if third == -1:
            return None
        line_number = line[second + 1 : third]
        if line_number.isdigit():
            return line[:second], int(line_number), line[third + 1 :].lstrip()
        second = line.find(":", second + 1)
    return None


def _select_search_matches(matches: list[_SearchMatch], *, max_matches: int) -> list[_SearchMatch]:
    if len(matches) <= max_matches:
        return list(matches)

    selected: list[_SearchMatch] = [matches[0]]
    if max_matches > 1:
        selected.append(matches[-1])

    for match in sorted(matches, key=_search_match_score, reverse=True):
        if len(selected) >= max_matches:
            break
        if match not in selected:
            selected.append(match)

    return sorted(selected, key=lambda item: item.line_number)


def _search_match_score(match: _SearchMatch) -> int:
    text = match.text.lower()
    score = 0
    for keyword in ("error", "failed", "fail", "traceback", "exception", "todo", "fixme", "warning"):
        if keyword in text:
            score += 10
    if "sentinel" in text:
        score += 20
    if re.search(r"\b[A-Z][A-Z0-9_]{6,}\b", match.text):
        score += 12
    score += min(3, len(match.text) // 80)
    return score
