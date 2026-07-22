"""Stable protocol ports for agent orchestration boundaries."""

from __future__ import annotations

from typing import Protocol

from firstcoder.context.manager import ContextCompactRequest, ContextCompactResult


class ContextManagerLike(Protocol):
    """Minimal context-window manager surface used by AgentLoop."""

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...
