"""Resolve discovered skills and render the model-visible catalog."""

from __future__ import annotations

from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillSource

SKILL_CATALOG_MAX_CHARS = 8_000
SKILL_DESCRIPTION_MAX_CHARS = 240
SKILL_LOAD_INSTRUCTION = "Use load_skill(name, args?) to load full instructions when needed."
SKILL_CATALOG_TRUNCATED = "Skill catalog truncated: not every skill name fits the catalog budget."


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

    skills = resolve_skill_catalog(catalog).skills
    if not skills:
        return SKILL_LOAD_INSTRUCTION

    footer = SKILL_LOAD_INSTRUCTION
    name_lines = [f"- {skill.name}:" for skill in skills]
    # Reserve one separator space per skill because normal rows are rendered as
    # ``- name: description``. This keeps the final string within the budget.
    fixed_cost = _joined_length([*name_lines, footer]) + len(skills)
    if fixed_cost > SKILL_CATALOG_MAX_CHARS:
        return _render_name_only_prefix(name_lines)

    description_budget = SKILL_CATALOG_MAX_CHARS - fixed_cost
    per_skill_limit = min(
        SKILL_DESCRIPTION_MAX_CHARS,
        description_budget // len(skills),
    )
    lines = [
        _catalog_line(skill, description_limit=per_skill_limit)
        for skill in skills
    ]
    return "\n".join([*lines, footer])


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


def _joined_length(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def _catalog_line(skill: SkillDefinition, *, description_limit: int) -> str:
    prefix = f"- {skill.name}:"
    description = " ".join(skill.description.split()) or "No description provided."
    description = _truncate_description(description, description_limit)
    return f"{prefix} {description}" if description else prefix


def _truncate_description(description: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(description) <= limit:
        return description
    if limit <= 3:
        return "." * limit
    return description[: limit - 3].rstrip() + "..."


def _render_name_only_prefix(name_lines: list[str]) -> str:
    footer = [SKILL_CATALOG_TRUNCATED, SKILL_LOAD_INSTRUCTION]
    lines: list[str] = []
    for line in name_lines:
        if _joined_length([*lines, line, *footer]) > SKILL_CATALOG_MAX_CHARS:
            break
        lines.append(line)
    return "\n".join([*lines, *footer])
