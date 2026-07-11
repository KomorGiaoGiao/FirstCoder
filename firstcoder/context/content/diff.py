"""git_diff / unified diff 输出的确定性压缩器。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart


@dataclass(slots=True)
class GitDiffRouteCompressor:
    max_context_lines: int = 2
    max_files: int = 20

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        lines = part.content.splitlines()
        if not any(line.startswith(("diff --git ", "--- a/", "+++ b/", "@@ ")) for line in lines):
            return None

        compressed: list[str] = ["[Git diff compacted]"]
        files_affected = 0
        additions = 0
        deletions = 0
        hunks = 0
        context_kept = 0
        context_omitted = 0
        visible_files = 0
        skip_current_file = False
        has_active_file = False

        for line in lines:
            if line.startswith("diff --git "):
                files_affected += 1
                has_active_file = True
                skip_current_file = visible_files >= self.max_files
                if skip_current_file:
                    continue
                visible_files += 1
                compressed.append("")
                compressed.append(line)
                continue

            if skip_current_file:
                continue

            if line.startswith(("--- ", "+++ ")):
                if not has_active_file:
                    files_affected += 1
                    has_active_file = True
                    skip_current_file = visible_files >= self.max_files
                    if skip_current_file:
                        continue
                    visible_files += 1
                compressed.append(line)
                continue

            if line.startswith("@@ "):
                hunks += 1
                compressed.append(line)
                continue

            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
                compressed.append(line)
                continue

            if line.startswith("-") and not line.startswith("---"):
                deletions += 1
                compressed.append(line)
                continue

            if line.startswith(" "):
                if _is_important_context(line) or context_kept < self.max_context_lines:
                    compressed.append(line)
                    context_kept += 1
                else:
                    context_omitted += 1
                continue

            compressed.append(line)

        hidden_files = max(0, files_affected - visible_files)
        if context_omitted:
            compressed.append(f"[... omitted {context_omitted} context lines]")
        if hidden_files:
            compressed.append(f"[... omitted {hidden_files} diff files]")

        return RouteCompactResult(
            content="\n".join(compressed).strip(),
            content_type=RouteContentType.GIT_DIFF,
            compacted_by="l2_git_diff",
            metadata={
                "diff_files_affected": files_affected,
                "diff_hidden_files": hidden_files,
                "diff_additions": additions,
                "diff_deletions": deletions,
                "diff_hunks": hunks,
                "diff_context_lines_kept": context_kept,
                "diff_context_lines_omitted": context_omitted,
            },
        )


def _is_important_context(line: str) -> bool:
    lowered = line.lower()
    return any(keyword in lowered for keyword in ("def ", "class ", "function ", "todo", "fixme", "error"))
