"""Cooperative cancellation primitives for agent turns."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(slots=True)
class CancellationToken:
    """Small thread-safe cancellation flag shared by UI, loop, and tools."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise AgentCancelledError()


class AgentCancelledError(RuntimeError):
    """Raised when an agent turn is interrupted by the user."""

    def __init__(self, message: str = "Agent turn was interrupted.") -> None:
        super().__init__(message)


_LOCAL = threading.local()


def current_cancellation_token() -> CancellationToken | None:
    return getattr(_LOCAL, "token", None)


class cancellation_context:
    """Temporarily expose a cancellation token to synchronous tool executors."""

    def __init__(self, token: CancellationToken | None) -> None:
        self.token = token
        self.previous: CancellationToken | None = None

    def __enter__(self) -> None:
        self.previous = current_cancellation_token()
        _LOCAL.token = self.token

    def __exit__(self, exc_type, exc, tb) -> None:
        _LOCAL.token = self.previous
