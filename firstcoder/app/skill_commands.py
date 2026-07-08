"""Skill-related slash commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firstcoder.app.commands import CommandResult
from firstcoder.skills.models import SkillCatalog, SkillDefinition


@dataclass(slots=True)
class SkillCommandHandler:
    """Handle `/skills` and `/skill <name>`."""

    catalog_provider: Callable[[], SkillCatalog]

    def handle(self, text: str) -> CommandResult:
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        parts = command.split()
        name = parts[0]
        args = parts[1:]
        if name == "/skills":
            return CommandResult(handled=True, output=self._list_skills())
        if name == "/skill":
            return CommandResult(handled=True, output=self._show_skill(args))
        return CommandResult(handled=False)

    def _list_skills(self) -> str:
        catalog = self.catalog_provider()
        if not catalog.skills:
            return "No skills."
        lines = ["Skills:"]
        for skill in catalog.skills:
            description = f" - {skill.description}" if skill.description else ""
            lines.append(f"- {skill.name} {skill.scope} {skill.path}{description}")
        return "\n".join(lines)

    def _show_skill(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /skill <name>"
        query = args[0].lower()
        catalog = self.catalog_provider()
        skill = _find_skill(catalog.skills, query)
        if skill is None:
            return f"Skill not found: {args[0]}"
        return "\n".join(
            [
                f"Skill: {skill.name}",
                f"Scope: {skill.scope}",
                f"Source: {skill.source.value}",
                f"Root: {skill.root}",
                f"Path: {skill.path}",
                f"Description: {skill.description or '<none>'}",
                f"Triggers: {', '.join(skill.triggers) if skill.triggers else '<none>'}",
            ]
        )


def _find_skill(skills: list[SkillDefinition], query: str) -> SkillDefinition | None:
    for skill in skills:
        if skill.name.lower() == query or skill.path.lower() == query:
            return skill
    for skill in skills:
        if query in skill.name.lower() or query in skill.path.lower():
            return skill
    return None
