"""Compatibility re-exports for cancellation primitives.

New code should import from `firstcoder.runtime.cancellation`.
"""

from __future__ import annotations

from firstcoder.runtime.cancellation import (
    AgentCancelledError,
    CancellationToken,
    cancellation_context,
    current_cancellation_token,
)

__all__ = [
    "AgentCancelledError",
    "CancellationToken",
    "cancellation_context",
    "current_cancellation_token",
]
