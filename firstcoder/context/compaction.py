"""L1-L3 程序化上下文压缩 pipeline。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from firstcoder.context.archive import ArchiveIntegrityError, ToolResultArchive
from firstcoder.context.checkpoint import CheckpointIndex
from firstcoder.context.content.build import BuildOutputRouteCompressor
from firstcoder.context.content.code import SourceCodeRouteCompressor
from firstcoder.context.content.compressors import PlainTextRouteCompressor, compact_old_task_part
from firstcoder.context.content.detector import (
    is_already_compacted,
    is_old_task_part,
)
from firstcoder.context.content.diff import GitDiffRouteCompressor
from firstcoder.context.content.html import HtmlRouteCompressor
from firstcoder.context.content.json import JsonRouteCompressor
from firstcoder.context.content.router import RouteCompactRouter, RouteContentType
from firstcoder.context.content.search import SearchResultsRouteCompressor
from firstcoder.context.identity import session_view_fingerprint
from firstcoder.context.models import AgentMessage, MessagePart, SessionView, latest_user_message_id, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.tool_lifecycle import (
    ToolResultLifecycle,
    ToolResultLifecycleRecord,
    index_tool_result_lifecycles,
)
from firstcoder.context.versions import COMPACTION_STRATEGY_VERSION, CONTEXT_EVENT_SCHEMA_VERSION

CompactionLevel = Literal["l1", "l2", "l3"]


@dataclass(slots=True)
class CompactionRequest:
    view: SessionView
    active_task_hash: str | None
    target_tokens: int
    current_turn: int
    enabled_levels: tuple[CompactionLevel, ...] = ("l1", "l2", "l3")
    required_levels: tuple[CompactionLevel, ...] = ()
    l2_result_target_tokens: int | None = None
    force_route_current_text: bool = False
    force_old_task_compaction: bool = False


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
    lifecycle_counts: dict[str, int] = field(default_factory=dict)
    level_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    archive_ids: list[str] = field(default_factory=list)


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
        input_fingerprint = session_view_fingerprint(request.view)
        before_tokens = _estimate_view_tokens(view)
        lifecycle_records = index_tool_result_lifecycles(
            _effective_tail_messages(view),
            current_turn=request.current_turn,
        )
        lifecycle_counts = _lifecycle_counts(lifecycle_records)
        required_levels = set(request.required_levels).intersection(request.enabled_levels)
        per_result_target = _per_result_target(
            request.l2_result_target_tokens,
            fallback=self.large_tool_result_tokens,
        )

        has_l3_mandatory_candidates = _has_l3_mandatory_candidates(
            _effective_tail_messages(view),
            lifecycle_records=lifecycle_records,
            current_turn=request.current_turn,
        )
        has_l3_per_result_pressure = _has_l3_per_result_pressure(
            _effective_tail_messages(view),
            lifecycle_records=lifecycle_records,
            current_turn=request.current_turn,
            per_result_target=per_result_target,
        )

        if (
            before_tokens <= request.target_tokens
            and not request.force_old_task_compaction
            and not required_levels
            and not ("l3" in request.enabled_levels and has_l3_mandatory_candidates)
            and not ({"l2", "l3"}.intersection(request.enabled_levels) and has_l3_per_result_pressure)
        ):
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
                    lifecycle_counts=lifecycle_counts,
                ),
            )

        levels_attempted: list[str] = []
        replacements: list[dict[str, object]] = []
        level_metrics: dict[str, dict[str, int]] = {}
        stopped_at = "not_reached"

        for level_index, level in enumerate(request.enabled_levels):
            levels_attempted.append(level)
            before_level_tokens = _estimate_view_tokens(view)
            level_replacements = self._apply_level(
                view,
                request=request,
                level=level,
                lifecycle_records=lifecycle_records,
            )
            replacements.extend(level_replacements)
            after_level_tokens = _estimate_view_tokens(view)
            level_metrics[level] = {
                "before_tokens": before_level_tokens,
                "after_tokens": after_level_tokens,
                "saved_tokens": max(0, before_level_tokens - after_level_tokens),
                "changed_parts": len(level_replacements),
            }
            remaining_levels = request.enabled_levels[level_index + 1 :]
            if (
                after_level_tokens <= request.target_tokens
                and not required_levels.intersection(remaining_levels)
                and not (
                    "l3" in remaining_levels
                    and (
                        _has_l3_mandatory_candidates(
                            _effective_tail_messages(view),
                            lifecycle_records=lifecycle_records,
                            current_turn=request.current_turn,
                        )
                        or _has_l3_per_result_pressure(
                            _effective_tail_messages(view),
                            lifecycle_records=lifecycle_records,
                            current_turn=request.current_turn,
                            per_result_target=per_result_target,
                        )
                    )
                )
            ):
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
                lifecycle_counts=lifecycle_counts,
                level_metrics=level_metrics,
                archive_ids=_archive_ids_from_replacements(replacements),
            ),
        )

    def _apply_level(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        level: CompactionLevel,
        lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    ) -> list[dict[str, object]]:
        if level == "l1":
            return self._apply_l1(
                view,
                active_task_hash=request.active_task_hash,
                current_turn=request.current_turn,
                force_old_task_compaction=request.force_old_task_compaction,
            )
        if level == "l2":
            return self._apply_l2(
                view,
                request=request,
                lifecycle_records=lifecycle_records,
            )
        if level == "l3":
            return self._apply_l3(
                view,
                request=request,
                active_task_hash=request.active_task_hash,
                current_turn=request.current_turn,
                lifecycle_records=lifecycle_records,
            )
        return []

    def _apply_l1(
        self,
        view: SessionView,
        *,
        active_task_hash: str | None,
        current_turn: int,
        force_old_task_compaction: bool,
    ) -> list[dict[str, object]]:
        """Trim only old-task ordinary dialogue that is safe to forget.

        L1 eligibility is partly a property of the enclosing message.  In
        particular, text in an assistant tool-call message must not be trimmed
        independently: it is part of the provider-visible tool transaction.
        """

        changed: list[dict[str, object]] = []
        tail_messages = _effective_tail_messages(view)
        latest_user_id = latest_user_message_id(tail_messages)
        for message in tail_messages:
            if message.role not in {"user", "assistant"}:
                continue
            if message.id == latest_user_id:
                continue
            if message.role == "assistant" and any(part.kind == "tool_call" for part in message.parts):
                continue
            for index, part in enumerate(message.parts):
                if not is_old_task_part(part, active_task_hash=active_task_hash):
                    continue
                if not force_old_task_compaction and not _is_cold_old_task_part(
                    part,
                    current_turn=current_turn,
                    cold_turn_distance=self.cold_turn_distance,
                ):
                    continue
                compacted = compact_old_task_part(part)
                if _replace_l1_trimmed(message.parts, index, compacted):
                    changed.append(_replacement_event(message_id=message.id, source=part, replacement=compacted))
        return changed

    def _apply_l2(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    ) -> list[dict[str, object]]:
        changed: list[dict[str, object]] = []
        archive = ToolResultArchive(self.root)
        router = _make_route_router(preview_chars=self.cold_preview_chars)
        for message in _effective_tail_messages(view):
            for index, part in enumerate(message.parts):
                lifecycle = lifecycle_records.get((message.id, part.id))
                if not _should_route_compact_l2_part(
                    part,
                    lifecycle=lifecycle,
                    current_turn=request.current_turn,
                ):
                    continue

                compacted = router.compact_part(part)
                if compacted is None:
                    continue
                try:
                    # This is backing, not L3 eviction: archive the exact raw
                    # bytes before the route result becomes visible in the view.
                    record = archive.store_original(view.session_id, part)
                except (ArchiveIntegrityError, OSError, ValueError):
                    continue

                assert lifecycle is not None
                compacted.metadata.update(
                    {
                        "archive_id": record.archive_id,
                        "original_content_sha256": record.content_sha256,
                        "original_tokens": record.original_tokens,
                        "replacement_tokens": estimate_text_tokens(compacted.content),
                        "lifecycle": lifecycle.lifecycle.value,
                        "lifecycle_reason": lifecycle.reason,
                        "compaction_state": "l2_route_compacted",
                        "compacted_by": _l2_compacted_by(compacted.metadata.get("compacted_by")),
                    }
                )
                if _replace_if_smaller(message.parts, index, compacted):
                    changed.append(_replacement_event(message_id=message.id, source=part, replacement=compacted))
        return changed

    def _apply_l3(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        active_task_hash: str | None,
        current_turn: int,
        lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    ) -> list[dict[str, object]]:
        changed: list[dict[str, object]] = []
        archive = ToolResultArchive(self.root)
        del active_task_hash
        candidates = _l3_candidates(
            _effective_tail_messages(view),
            lifecycle_records=lifecycle_records,
            current_turn=current_turn,
            target_tokens=request.target_tokens,
            per_result_target=_per_result_target(
                request.l2_result_target_tokens,
                fallback=self.large_tool_result_tokens,
            ),
        )
        for candidate in candidates:
            if not candidate.mandatory and not candidate.over_per_result_target and _estimate_view_tokens(view) <= request.target_tokens:
                break

            part = candidate.message.parts[candidate.part_index]
            # A preceding candidate may have transformed this part in future
            # refactors.  Re-check before touching durable backing.
            if not _can_archive_l3_part(
                part,
                lifecycle=candidate.lifecycle,
                current_turn=current_turn,
            ):
                continue
            try:
                record = _l3_backing_record(archive, view.session_id, part)
                compacted = archive.make_placeholder(
                    part,
                    record,
                    lifecycle=candidate.lifecycle.lifecycle.value,
                    summary=_lifecycle_summary(part, candidate.lifecycle),
                    key_errors=_lifecycle_key_errors(part),
                )
            except (ArchiveIntegrityError, OSError, ValueError):
                # Archive backing is an all-or-nothing safety boundary: if
                # persistence or validation fails, retain the current part.
                continue
            compacted.metadata.update(
                {
                    "lifecycle": candidate.lifecycle.lifecycle.value,
                    "lifecycle_reason": candidate.lifecycle.reason,
                    "replacement_tokens": estimate_text_tokens(compacted.content),
                }
            )
            if _replace_if_smaller(candidate.message.parts, candidate.part_index, compacted):
                changed.append(
                    _replacement_event(
                        message_id=candidate.message.id,
                        source=part,
                        replacement=compacted,
                    )
                )
        return changed


def _estimate_view_tokens(view: SessionView) -> int:
    return sum(estimate_text_tokens(part.content) for message in view.messages for part in message.parts)


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


def _replace_l1_trimmed(parts: list[MessagePart], index: int, trimmed: MessagePart) -> bool:
    """Apply L1 even when the resulting empty content has zero tokens."""

    if parts[index].content == trimmed.content and parts[index].metadata == trimmed.metadata:
        return False
    parts[index] = trimmed
    return True


def _replace_if_smaller(parts: list[MessagePart], index: int, compacted: MessagePart) -> bool:
    original = parts[index]
    if estimate_text_tokens(compacted.content) >= estimate_text_tokens(original.content):
        return False
    parts[index] = compacted
    return True


def _is_cold_old_task_part(
    part: MessagePart,
    *,
    current_turn: int,
    cold_turn_distance: int,
) -> bool:
    created_turn = part.metadata.get("created_turn")
    return isinstance(created_turn, int) and not isinstance(created_turn, bool) and current_turn - created_turn >= cold_turn_distance


def _archive_ids_from_replacements(replacements: list[dict[str, object]]) -> list[str]:
    archive_ids: list[str] = []
    for replacement in replacements:
        replacement_part = replacement.get("replacement_part")
        if not isinstance(replacement_part, dict):
            continue
        metadata = replacement_part.get("metadata")
        if not isinstance(metadata, dict):
            continue
        archive_id = metadata.get("archive_id")
        if isinstance(archive_id, str) and archive_id and archive_id not in archive_ids:
            archive_ids.append(archive_id)
    return archive_ids


def _lifecycle_counts(
    lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
) -> dict[str, int]:
    counts = {lifecycle.value: 0 for lifecycle in ToolResultLifecycle}
    for record in lifecycle_records.values():
        counts[record.lifecycle.value] += 1
    return counts


def _make_route_router(*, preview_chars: int) -> RouteCompactRouter:
    json_compressor = JsonRouteCompressor()
    return RouteCompactRouter(
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
        preview_chars=preview_chars,
    )


def _l2_compacted_by(value: object) -> str:
    """Translate the existing route labels at the L2 ownership boundary.

    Content compressors deliberately remain independently usable.  The
    pipeline is where their output acquires its L2 semantic label.
    """

    label = str(value or "l2_route")
    if label.startswith("l3_"):
        return f"l2_{label[3:]}"
    return label


def _should_route_compact_l2_part(
    part: MessagePart,
    *,
    lifecycle: ToolResultLifecycleRecord | None,
    current_turn: int,
) -> bool:
    if part.kind != "tool_result" or lifecycle is None or is_already_compacted(part):
        return False
    if _is_retrieval_protected(part, current_turn=current_turn):
        return False
    return lifecycle.lifecycle is ToolResultLifecycle.DERIVED


def _is_retrieval_protected(part: MessagePart, *, current_turn: int) -> bool:
    metadata = part.metadata
    retrieval_metadata: dict[str, object] = metadata
    if metadata.get("archive_retrieval") is not True:
        nested_data = metadata.get("data")
        if not isinstance(nested_data, dict) or nested_data.get("archive_retrieval") is not True:
            return False
        retrieval_metadata = nested_data
    protected_until_turn = retrieval_metadata.get("compaction_protected_until_turn")
    return isinstance(protected_until_turn, int) and not isinstance(protected_until_turn, bool) and protected_until_turn >= current_turn


def _lifecycle_summary(part: MessagePart, lifecycle: ToolResultLifecycleRecord) -> str:
    tool_name = str(part.metadata.get("tool_name") or "tool").replace("\n", " ").strip() or "tool"
    return f"{tool_name} result is {lifecycle.lifecycle.value}: {lifecycle.reason}."


def _lifecycle_key_errors(part: MessagePart) -> tuple[str, ...]:
    error = part.metadata.get("error")
    return (error,) if isinstance(error, str) and error.strip() else ()


@dataclass(frozen=True, slots=True)
class _L3Candidate:
    message: AgentMessage
    part_index: int
    lifecycle: ToolResultLifecycleRecord
    mandatory: bool
    over_per_result_target: bool
    priority: int
    tokens: int
    created_turn: int
    tail_index: int


def _has_l3_mandatory_candidates(
    messages: list[AgentMessage],
    *,
    lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    current_turn: int,
) -> bool:
    for message in messages:
        for part in message.parts:
            lifecycle = lifecycle_records.get((message.id, part.id))
            if _is_l3_mandatory(lifecycle) and _can_archive_l3_part(
                part,
                lifecycle=lifecycle,
                current_turn=current_turn,
            ):
                return True
    return False


def _has_l3_per_result_pressure(
    messages: list[AgentMessage],
    *,
    lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    current_turn: int,
    per_result_target: int | None,
) -> bool:
    """Whether an eligible derived result needs an L2/L3 pass below budget."""

    if per_result_target is None:
        return False
    for message in messages:
        for part in message.parts:
            lifecycle = lifecycle_records.get((message.id, part.id))
            if (
                lifecycle is not None
                and lifecycle.lifecycle is ToolResultLifecycle.DERIVED
                and _can_archive_l3_part(part, lifecycle=lifecycle, current_turn=current_turn)
                and estimate_text_tokens(part.content) > per_result_target
            ):
                return True
    return False


def _per_result_target(value: object, *, fallback: int) -> int | None:
    """Resolve a positive per-result budget without treating bool as int."""

    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback > 0:
        return fallback
    return None


def _l3_candidates(
    messages: list[AgentMessage],
    *,
    lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    current_turn: int,
    target_tokens: int,
    per_result_target: int | None,
) -> list[_L3Candidate]:
    """Return deterministic tool-result-only L3 candidates.

    Mandatory lifecycle cleanup is selected regardless of the overall target.
    Derived output is optional: it is selected when an individual result still
    exceeds its L2 budget or when the current context remains above target.
    """

    del target_tokens  # Selection below-budget is decided during application.
    candidates: list[_L3Candidate] = []
    tail_index = 0
    for message in messages:
        for part_index, part in enumerate(message.parts):
            lifecycle = lifecycle_records.get((message.id, part.id))
            if lifecycle is None or not _can_archive_l3_part(
                part,
                lifecycle=lifecycle,
                current_turn=current_turn,
            ):
                tail_index += 1
                continue

            tokens = estimate_text_tokens(part.content)
            mandatory = _is_l3_mandatory(lifecycle)
            over_per_result_target = per_result_target is not None and lifecycle.lifecycle is ToolResultLifecycle.DERIVED and tokens > per_result_target
            if mandatory:
                priority = _l3_priority(lifecycle.lifecycle)
            elif over_per_result_target:
                priority = _l3_priority(lifecycle.lifecycle, over_per_result_target=True)
            elif lifecycle.lifecycle is ToolResultLifecycle.DERIVED:
                priority = _l3_priority(lifecycle.lifecycle)
            else:
                tail_index += 1
                continue

            created_turn = part.metadata.get("created_turn")
            candidates.append(
                _L3Candidate(
                    message=message,
                    part_index=part_index,
                    lifecycle=lifecycle,
                    mandatory=mandatory,
                    over_per_result_target=over_per_result_target,
                    priority=priority,
                    tokens=tokens,
                    created_turn=created_turn if isinstance(created_turn, int) and not isinstance(created_turn, bool) else 0,
                    tail_index=tail_index,
                )
            )
            tail_index += 1

    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.priority,
            -candidate.tokens,
            candidate.created_turn,
            candidate.tail_index,
        ),
    )


def _can_archive_l3_part(
    part: MessagePart,
    *,
    lifecycle: ToolResultLifecycleRecord | None,
    current_turn: int,
) -> bool:
    if part.kind != "tool_result" or lifecycle is None:
        return False
    # L3 may turn a raw result or its L2 projection into a placeholder.  It
    # must not consume a pinned/retrieved result or replay a legacy/terminal
    # compaction projection whose backing is not this L2 flow's raw record.
    state = str(part.metadata.get("compaction_state") or "raw")
    if state not in {"raw", "l2_route_compacted"}:
        return False
    if _is_retrieval_protected(part, current_turn=current_turn):
        return False
    return lifecycle.lifecycle in {
        ToolResultLifecycle.STALE,
        ToolResultLifecycle.SUPERSEDED,
        ToolResultLifecycle.DUPLICATE,
        ToolResultLifecycle.DERIVED,
    }


def _is_l3_mandatory(lifecycle: ToolResultLifecycleRecord | None) -> bool:
    return lifecycle is not None and lifecycle.lifecycle in {
        ToolResultLifecycle.DUPLICATE,
        ToolResultLifecycle.SUPERSEDED,
        ToolResultLifecycle.STALE,
    }


def _l3_priority(lifecycle: ToolResultLifecycle, *, over_per_result_target: bool = False) -> int:
    if lifecycle is ToolResultLifecycle.DUPLICATE:
        return 0
    if lifecycle is ToolResultLifecycle.SUPERSEDED:
        return 1
    if lifecycle is ToolResultLifecycle.STALE:
        return 2
    if over_per_result_target:
        return 3
    return 4


def _l3_backing_record(
    archive: ToolResultArchive,
    session_id: str,
    part: MessagePart,
):
    """Return raw backing for a candidate without archiving L2 text as raw.

    L2 retains its original archive id and payload.  A later L3 projection
    must use exactly that backing so `retrieve_archive` always returns the
    pre-route result rather than a compact derivative.
    """

    archive_id = part.metadata.get("archive_id")
    if isinstance(archive_id, str) and archive_id:
        record, _raw = archive.read(session_id, archive_id)
        return record
    return archive.store_original(session_id, part)
