"""Resolve discovered skills and render the model-visible catalog."""

from __future__ import annotations

from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillSource

SKILL_CATALOG_MAX_CHARS = 8_000
SKILL_DESCRIPTION_MAX_CHARS = 240
SKILL_LOAD_INSTRUCTION = "Use load_skill(name, args?) to load full instructions when needed."


def resolve_skill_catalog(catalog: SkillCatalog) -> SkillCatalog:
    """Return one deterministic effective definition for each skill name."""

    selected: dict[str, SkillDefinition] = {}
    for skill in sorted(catalog.skills, key=_resolution_key):
        selected.setdefault(skill.name, skill)
    return SkillCatalog(
        skills=[selected[name] for name in sorted(selected)],
        index_content=catalog.index_content,
    )


def render_skill_catalog(catalog: SkillCatalog) -> str:
    """Render whole catalog lines within the fixed system-prompt budget."""

    lines: list[str] = []
    used_chars = len(SKILL_LOAD_INSTRUCTION)
    for skill in resolve_skill_catalog(catalog).skills:
        line = _catalog_line(skill)
        added_chars = len(line) + 1
        if used_chars + added_chars > SKILL_CATALOG_MAX_CHARS:
            continue
        lines.append(line)
        used_chars += added_chars
    return "\n".join([*lines, SKILL_LOAD_INSTRUCTION])


def _resolution_key(skill: SkillDefinition) -> tuple[int, str, str, str]:
    return (_source_priority(skill.source), skill.name, skill.root, skill.path)


def _source_priority(source: SkillSource) -> int:
    priorities = {
        SkillSource.PROJECT_AGENT_SKILL: 0,
        SkillSource.PROJECT_MARKDOWN: 1,
        SkillSource.GLOBAL_AGENT_SKILL: 2,
        SkillSource.GLOBAL_MARKDOWN: 3,
    }
    return priorities[source]


def _catalog_line(skill: SkillDefinition) -> str:
    description = " ".join(skill.description.split()) or "No description provided."
    if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
        description = description[: SKILL_DESCRIPTION_MAX_CHARS - 3].rstrip() + "..."
    return f"- {skill.name}: {description}"
