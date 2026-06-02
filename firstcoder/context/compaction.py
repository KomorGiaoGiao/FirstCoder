"""L1-L3 程序化上下文压缩 pipeline。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from firstcoder.context.archive import ToolResultArchive
from firstcoder.context.content.compressors import compact_cold_text_part, compact_old_task_part
from firstcoder.context.content.detector import (
    is_current_task_cold_part,
    is_large_tool_result,
    is_old_task_part,
)
from firstcoder.context.identity import stable_json_hash
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.token_budget import estimate_text_tokens


CompactionLevel = Literal["l1", "l2", "l3"]


@dataclass(slots=True)
class CompactionRequest:
    view: SessionView
    active_task_hash: str | None
    target_tokens: int
    current_turn: int
    enabled_levels: tuple[CompactionLevel, ...] = ("l1", "l2", "l3")


@dataclass(slots=True)
class CompactionEvent:
    input_fingerprint: str
    before_tokens: int
    after_tokens: int
    levels_attempted: list[str]
    stopped_at: str
    changed_parts: int
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
                    noop=True,
                    deduped=deduped,
                ),
            )

        levels_attempted: list[str] = []
        changed_parts = 0
        stopped_at = "not_reached"

        for level in request.enabled_levels:
            levels_attempted.append(level)
            changed_parts += self._apply_level(view, request=request, level=level)
            after_level_tokens = _estimate_view_tokens(view)
            if after_level_tokens <= request.target_tokens:
                stopped_at = level
                break

        after_tokens = _estimate_view_tokens(view)
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
    ) -> int:
        if level == "l1":
            return self._apply_l1(view, active_task_hash=request.active_task_hash)
        if level == "l2":
            return self._apply_l2(view)
        if level == "l3":
            return self._apply_l3(
                view,
                active_task_hash=request.active_task_hash,
                current_turn=request.current_turn,
            )
        return 0

    def _apply_l1(self, view: SessionView, *, active_task_hash: str | None) -> int:
        changed = 0
        for message in view.messages:
            for index, part in enumerate(message.parts):
                if is_old_task_part(part, active_task_hash=active_task_hash):
                    if _replace_if_smaller(message.parts, index, compact_old_task_part(part)):
                        changed += 1
        return changed

    def _apply_l2(self, view: SessionView) -> int:
        changed = 0
        archive = ToolResultArchive(self.root)
        for message in view.messages:
            for index, part in enumerate(message.parts):
                if is_large_tool_result(part, min_tokens=self.large_tool_result_tokens):
                    message.parts[index] = archive.archive_part(session_id=view.session_id, part=part)
                    changed += 1
        return changed

    def _apply_l3(
        self,
        view: SessionView,
        *,
        active_task_hash: str | None,
        current_turn: int,
    ) -> int:
        changed = 0
        for message in view.messages:
            for index, part in enumerate(message.parts):
                if is_current_task_cold_part(
                    part,
                    active_task_hash=active_task_hash,
                    current_turn=current_turn,
                    cold_turn_distance=self.cold_turn_distance,
                ):
                    compacted = compact_cold_text_part(
                        part,
                        preview_chars=self.cold_preview_chars,
                    )
                    if _replace_if_smaller(message.parts, index, compacted):
                        changed += 1
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


def _replace_if_smaller(parts: list[MessagePart], index: int, compacted: MessagePart) -> bool:
    original = parts[index]
    if estimate_text_tokens(compacted.content) >= estimate_text_tokens(original.content):
        return False
    parts[index] = compacted
    return True
