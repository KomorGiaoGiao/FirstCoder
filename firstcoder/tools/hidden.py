"""Tools that should stay out of normal user-visible activity streams."""

from __future__ import annotations

# Internal control-plane tools: useful to the agent, noisy for humans.
HIDDEN_TOOL_STATUS_NAMES: frozenset[str] = frozenset({"task_boundary"})
