"""Skill system data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from firstcoder.context.identity import content_fingerprint, stable_json_hash


class SkillSource(StrEnum):
    PROJECT_MARKDOWN = "project_markdown"
    PROJECT_AGENT_SKILL = "project_agent_skill"
    GLOBAL_MARKDOWN = "global_markdown"
    GLOBAL_AGENT_SKILL = "global_agent_skill"


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    path: str
    source: SkillSource
    root: str
    description: str = ""
    triggers: tuple[str, ...] = ()

    @property
    def scope(self) -> str:
        if self.source in {SkillSource.PROJECT_MARKDOWN, SkillSource.PROJECT_AGENT_SKILL}:
            return "project"
        return "global"


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    skill: SkillDefinition
    content: str
    required_files: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return content_fingerprint(self.content)

    @property
    def bytes(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    skills: list[SkillDefinition] = field(default_factory=list)
    index_content: str = ""

    def resolved(self) -> "SkillCatalog":
        """Return the unique effective catalog used by runtime consumers."""

        from firstcoder.skills.catalog import resolve_skill_catalog

        return resolve_skill_catalog(self)

    @property
    def fingerprint(self) -> str:
        return stable_json_hash(
            {
                "index_content": self.index_content,
                "skills": [
                    {
                        "name": skill.name,
                        "path": skill.path,
                        "source": skill.source.value,
                        "root": skill.root,
                        "description": skill.description,
                        "triggers": list(skill.triggers),
                    }
                    for skill in self.skills
                ],
            }
        )
