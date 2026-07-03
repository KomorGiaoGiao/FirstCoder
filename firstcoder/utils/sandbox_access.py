"""Shared sandbox access state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SandboxAccessMode(StrEnum):
    PROJECT = "project"
    UNRESTRICTED = "unrestricted"


@dataclass(slots=True)
class SandboxAccess:
    mode: SandboxAccessMode = SandboxAccessMode.PROJECT

    @property
    def unrestricted(self) -> bool:
        return self.mode == SandboxAccessMode.UNRESTRICTED
