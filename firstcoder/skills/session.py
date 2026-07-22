"""Session event helpers for skill audit records."""

from __future__ import annotations

from firstcoder.context.writer import SessionEventWriter
from firstcoder.skills.models import LoadedSkill, SkillDefinition


def append_skill_selected(writer: SessionEventWriter, skill: SkillDefinition, *, reason: str, confidence: str) -> None:
    writer.append_event(
        "skill_selected",
        {
            "skill_name": skill.name,
            "skill_scope": skill.scope,
            "skill_source": skill.source.value,
            "skill_root": skill.root,
            "skill_path": skill.path,
            "reason": reason,
            "confidence": confidence,
            "turn_id": writer.current_turn,
        },
    )


def append_skill_loaded(writer: SessionEventWriter, loaded: LoadedSkill) -> None:
    skill = loaded.skill
    writer.append_event(
        "skill_loaded",
        {
            "skill_name": skill.name,
            "skill_scope": skill.scope,
            "skill_source": skill.source.value,
            "skill_root": skill.root,
            "skill_path": skill.path,
            "content_hash": loaded.content_hash,
            "bytes": loaded.bytes,
            "required_files": list(loaded.required_files),
            "turn_id": writer.current_turn,
        },
    )
