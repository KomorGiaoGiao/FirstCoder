"""Skill file loading."""

from __future__ import annotations

import re
from pathlib import Path

from firstcoder.skills.models import LoadedSkill, SkillDefinition


class SkillLoadError(ValueError):
    """A skill could not be loaded safely."""


class SkillLoader:
    def load(self, skill: SkillDefinition) -> LoadedSkill:
        path = self._resolve_path(skill)
        if not path.exists() or not path.is_file():
            raise SkillLoadError(f"skill file does not exist: {skill.path}")
        content = path.read_text(encoding="utf-8")
        return self.load_from_content(skill, content)

    def load_from_content(self, skill: SkillDefinition, content: str) -> LoadedSkill:
        return LoadedSkill(
            skill=skill,
            content=content,
            required_files=_extract_required_files(content),
        )

    def _resolve_path(self, skill: SkillDefinition) -> Path:
        root = Path(skill.root).resolve()
        path = (root / skill.path).resolve()
        if not path.is_relative_to(root):
            raise SkillLoadError(f"skill path escapes root: {skill.path}")
        return path

def _extract_required_files(content: str) -> list[str]:
    required: list[str] = []
    in_required_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if _is_required_heading(stripped):
            in_required_block = True
            _append_required_paths(required, stripped)
            continue
        if in_required_block and stripped.startswith("#"):
            break
        if not in_required_block:
            continue
        _append_required_paths(required, stripped)
    return required


def _append_required_paths(required: list[str], line: str) -> None:
    for match in re.findall(r"`([^`]+\.(?:md|yaml|yml|json|py|txt))`", line):
        if match not in required:
            required.append(match)


def _is_required_heading(line: str) -> bool:
    normalized = line.lower()
    return any(
        marker in normalized
        for marker in [
            "必须读取",
            "必须先读",
            "必读文件",
            "预读文件",
            "required files",
            "must read",
        ]
    )
