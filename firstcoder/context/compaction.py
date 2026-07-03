"""L1-L3 程序化上下文压缩 pipeline。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from firstcoder.context.archive import ToolResultArchive
from firstcoder.context.checkpoint import CheckpointIndex
from firstcoder.context.content.build import BuildOutputRouteCompressor
from firstcoder.context.content.code import SourceCodeRouteCompressor
from firstcoder.context.content.compressors import PlainTextRouteCompressor, compact_old_task_part
from firstcoder.context.content.detector import (
    is_already_compacted,
    is_current_task_cold_part,
    is_large_tool_result,
    is_old_task_part,
)
from firstcoder.context.content.diff import GitDiffRouteCompressor
from firstcoder.context.content.html import HtmlRouteCompressor
from firstcoder.context.content.json import JsonRouteCompressor
from firstcoder.context.content.router import RouteCompactRouter, RouteContentType
from firstcoder.context.content.search import SearchResultsRouteCompressor
from firstcoder.context.identity import stable_json_hash
from firstcoder.context.models import AgentMessage, MessagePart, SessionView, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import COMPACTION_STRATEGY_VERSION, CONTEXT_EVENT_SCHEMA_VERSION


CompactionLevel = Literal["l1", "l2", "l3"]


@dataclass(slots=True)
class CompactionRequest:
    view: SessionView
    active_task_hash: str | None
    target_tokens: int
    current_turn: int
    enabled_levels: tuple[CompactionLevel, ...] = ("l1", "l2", "l3")
    force_route_current_text: bool = False


@dataclass(slots=True)
class CompactionEvent:
    input_fingerprint: str
    before_tokens: int
    after_tokens: int
    levels_attempted: list[str]
    stopped_at: str
    changed_parts: int
    reason: str = "programmatic_compaction"
    target_tokens: int = 0
    source_part_ids: list[str] = field(default_factory=list)
    output_part_ids: list[str] = field(default_factory=list)
    replacements: list[dict[str, object]] = field(default_factory=list)
    checkpoint_id: str | None = None
    strategy_version: str = COMPACTION_STRATEGY_VERSION
    event_version: str = CONTEXT_EVENT_SCHEMA_VERSION
    llm_used: bool = False
    success: bool = True
    error: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    noop: bool = False
    deduped: bool = False


@dataclass(slots=True)
class CompactionResult:
    view: SessionView
    event: CompactionEvent


@dataclass(slots=True)
class CompactionPipeline:
    root: str | Path
    large_tool_result_tokens: int = 1200
    cold_turn_distance: int = 8
    cold_preview_chars: int = 160
    _seen_noop_fingerprints: set[str] = field(default_factory=set)

    def compact(self, request: CompactionRequest) -> CompactionResult:
        view = _clone_view(request.view)
        input_fingerprint = _view_fingerprint(request.view)
        before_tokens = _estimate_view_tokens(view)
        if before_tokens <= request.target_tokens:
            deduped = input_fingerprint in self._seen_noop_fingerprints
            self._seen_noop_fingerprints.add(input_fingerprint)
            return CompactionResult(
                view=view,
                event=CompactionEvent(
                    input_fingerprint=input_fingerprint,
                    before_tokens=before_tokens,
                    after_tokens=before_tokens,
                    levels_attempted=[],
                    stopped_at="already_within_budget",
                    changed_parts=0,
                    reason="already_within_budget",
                    target_tokens=request.target_tokens,
                    noop=True,
                    deduped=deduped,
                ),
            )

        levels_attempted: list[str] = []
        replacements: list[dict[str, object]] = []
        stopped_at = "not_reached"

        for level in request.enabled_levels:
            levels_attempted.append(level)
            replacements.extend(self._apply_level(view, request=request, level=level))
            after_level_tokens = _estimate_view_tokens(view)
            if after_level_tokens <= request.target_tokens:
                stopped_at = level
                break

        after_tokens = _estimate_view_tokens(view)
        changed_parts = len(replacements)
        noop = changed_parts == 0
        deduped = noop and input_fingerprint in self._seen_noop_fingerprints
        if noop:
            self._seen_noop_fingerprints.add(input_fingerprint)

        return CompactionResult(
            view=view,
            event=CompactionEvent(
                input_fingerprint=input_fingerprint,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                levels_attempted=levels_attempted,
                stopped_at=stopped_at,
                changed_parts=changed_parts,
                reason=stopped_at,
                target_tokens=request.target_tokens,
                source_part_ids=[str(replacement["source_part_id"]) for replacement in replacements],
                output_part_ids=[str(replacement["replacement_part"]["id"]) for replacement in replacements],
                replacements=replacements,
                noop=noop,
                deduped=deduped,
            ),
        )

    def _apply_level(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        level: CompactionLevel,
    ) -> list[dict[str, object]]:
        if level == "l1":
            return self._apply_l1(view, active_task_hash=request.active_task_hash)
        if level == "l2":
            return self._apply_l2(view)
        if level == "l3":
            return self._apply_l3(
                view,
                request=request,
                active_task_hash=request.active_task_hash,
                current_turn=request.current_turn,
            )
        return []

    def _apply_l1(self, view: SessionView, *, active_task_hash: str | None) -> list[dict[str, object]]:
        changed: list[dict[str, object]] = []
        for message in _effective_tail_messages(view):
            for index, part in enumerate(message.parts):
                if is_old_task_part(part, active_task_hash=active_task_hash):
                    compacted = compact_old_task_part(part)
                    if _replace_if_smaller(message.parts, index, compacted):
                        changed.append(_replacement_event(message_id=message.id, source=part, replacement=compacted))
        return changed

    def _apply_l2(self, view: SessionView) -> list[dict[str, object]]:
        changed: list[dict[str, object]] = []
        archive = ToolResultArchive(self.root)
        for message in _effective_tail_messages(view):
            for index, part in enumerate(message.parts):
                if is_large_tool_result(part, min_tokens=self.large_tool_result_tokens):
                    archived = archive.archive_part(session_id=view.session_id, part=part)
                    message.parts[index] = archived
                    changed.append(_replacement_event(message_id=message.id, source=part, replacement=archived))
        return changed

    def _apply_l3(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        active_task_hash: str | None,
        current_turn: int,
    ) -> list[dict[str, object]]:
        changed: list[dict[str, object]] = []
        json_compressor = JsonRouteCompressor()
        router = RouteCompactRouter(
            compressors={
                RouteContentType.BUILD_OUTPUT: BuildOutputRouteCompressor(),
                RouteContentType.GIT_DIFF: GitDiffRouteCompressor(),
                RouteContentType.HTML: HtmlRouteCompressor(),
                RouteContentType.JSON_ARRAY: json_compressor,
                RouteContentType.JSON_OBJECT: json_compressor,
                RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(),
                RouteContentType.SOURCE_CODE: SourceCodeRouteCompressor(),
                RouteContentType.PLAIN_TEXT: PlainTextRouteCompressor(),
            },
            preview_chars=self.cold_preview_chars,
        )
        for message in _effective_tail_messages(view):
            for index, part in enumerate(message.parts):
                if _should_route_compact_part(
                    part,
                    request=request,
                    active_task_hash=active_task_hash,
                    current_turn=current_turn,
                    cold_turn_distance=self.cold_turn_distance,
                ):
                    compacted = router.compact_part(part)
                    if compacted is None:
                        continue
                    if _replace_if_smaller(message.parts, index, compacted):
                        changed.append(_replacement_event(message_id=message.id, source=part, replacement=compacted))
        return changed


def _estimate_view_tokens(view: SessionView) -> int:
    return sum(estimate_text_tokens(part.content) for message in view.messages for part in message.parts)


def _view_fingerprint(view: SessionView) -> str:
    return stable_json_hash(
        {
            "session_id": view.session_id,
            "messages": [message.to_dict() for message in view.messages],
        },
        length=24,
    )


def _clone_view(view: SessionView) -> SessionView:
    return SessionView(
        session_id=view.session_id,
        messages=[AgentMessage.from_dict(message.to_dict()) for message in view.messages],
        checkpoints=list(view.checkpoints),
        metadata=dict(view.metadata),
    )


def _effective_tail_messages(view: SessionView) -> list[AgentMessage]:
    """只让程序化压缩处理 latest checkpoint 之后的真实 tail。

    checkpoint 覆盖过的旧历史已经由 summary 表达；L1-L3 如果继续扫描旧 raw message，
    会和 ContextBuilder/L4 的 effective context 边界不一致。
    """

    checkpoint = CheckpointIndex(view.checkpoints).latest()
    if checkpoint is None:
        return view.messages

    for index, message in enumerate(view.messages):
        if message.id == checkpoint.tail_start_message_id:
            return view.messages[index:]
    raise ValueError(f"latest checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}")


def _replacement_event(*, message_id: str, source: MessagePart, replacement: MessagePart) -> dict[str, object]:
    return {
        "message_id": message_id,
        "source_part_id": source.id,
        "replacement_part": replacement.to_dict(),
    }


def _replace_if_smaller(parts: list[MessagePart], index: int, compacted: MessagePart) -> bool:
    original = parts[index]
    if estimate_text_tokens(compacted.content) >= estimate_text_tokens(original.content):
        return False
    parts[index] = compacted
    return True


def _should_route_compact_part(
    part: MessagePart,
    *,
    request: CompactionRequest,
    active_task_hash: str | None,
    current_turn: int,
    cold_turn_distance: int,
) -> bool:
    if is_current_task_cold_part(
        part,
        active_task_hash=active_task_hash,
        current_turn=current_turn,
        cold_turn_distance=cold_turn_distance,
    ):
        return True
    if not request.force_route_current_text:
        return False
    if is_already_compacted(part):
        return False
    return part.kind == "text"
