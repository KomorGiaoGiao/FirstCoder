"""Load one registered skill as an ordinary session tool result."""

from __future__ import annotations

from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ToolDefinition
from firstcoder.skills.loader import SkillLoadError, SkillLoader
from firstcoder.skills.models import SkillCatalog
from firstcoder.skills.session import append_skill_loaded, append_skill_selected
from firstcoder.tools.types import Tool, make_error_result, make_text_result
from firstcoder.utils.schema import object_schema


def create_load_skill_tool(catalog: SkillCatalog, writer: SessionEventWriter) -> Tool:
    resolved = catalog.resolved()
    skills_by_name = {skill.name: skill for skill in resolved.skills}

    def load_skill(*, name: str, args: str | None = None):
        skill = skills_by_name.get(name)
        if skill is None:
            available = ", ".join(skills_by_name) or "none"
            return make_error_result(
                "load_skill",
                f"Skill not found: {name}. Available skills: {available}",
                requested_name=name,
                available_skills=list(skills_by_name),
            )
        try:
            loaded = SkillLoader().load(skill)
        except (SkillLoadError, OSError, UnicodeError):
            return make_error_result(
                "load_skill",
                f"Unable to load skill: {name}",
                requested_name=name,
            )

        append_skill_selected(writer, skill, reason="model_tool_call", confidence="high")
        append_skill_loaded(writer, loaded)
        header = [f"Loaded skill: {skill.name}"]
        if args:
            header.append(f"Arguments: {args}")
        content = "\n".join([*header, "", loaded.content])
        return make_text_result(
            "load_skill",
            content,
            skill_name=skill.name,
            args=args,
            content_hash=loaded.content_hash,
            required_files=list(loaded.required_files),
        )

    parameters = object_schema(
        {
            "name": {"type": "string"},
            "args": {"type": "string"},
        },
        required=["name"],
    )
    parameters["additionalProperties"] = False
    return Tool(
        definition=ToolDefinition(
            name="load_skill",
            description="Load the full instructions for one available skill by exact name before following it.",
            parameters=parameters,
        ),
        executor=load_skill,
    )
