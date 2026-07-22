"""Session event helpers for skill audit records."""

from __future__ import annotations

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.skills.loader import SkillLoadError, SkillLoader
from firstcoder.skills.models import LoadedSkill, LoadedSkillRequiredFile, SkillCatalog, SkillRoutingDecision


def append_skill_selected(writer: SessionEventWriter, decision: SkillRoutingDecision) -> None:
    if decision.selected is None:
        return
    skill = decision.selected
    writer.append_event(
        "skill_selected",
        {
            "skill_name": skill.name,
            "skill_scope": skill.scope,
            "skill_source": skill.source.value,
            "skill_root": skill.root,
            "skill_path": skill.path,
            "reason": decision.reason,
            "confidence": decision.confidence,
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


def append_skill_required_file_loaded(writer: SessionEventWriter, required: LoadedSkillRequiredFile) -> None:
    skill = required.skill
    writer.append_event(
        "skill_required_file_loaded",
        {
            "skill_name": skill.name,
            "skill_scope": skill.scope,
            "skill_source": skill.source.value,
            "skill_root": skill.root,
            "skill_path": skill.path,
            "file_path": required.file_path,
            "content_hash": required.content_hash,
            "bytes": required.bytes,
            "turn_id": writer.current_turn,
        },
    )


def replay_loaded_skills(store: JsonlSessionStore, session_id: str, catalog: SkillCatalog) -> list[LoadedSkill]:
    loaded: list[LoadedSkill] = []
    for event in store.list_events(session_id):
        if event.type != "skill_loaded":
            continue
        skill_path = str(event.payload.get("skill_path") or "")
        skill_root = str(event.payload.get("skill_root") or "")
        skill = next(
            (candidate for candidate in catalog.skills if candidate.path == skill_path and candidate.root == skill_root),
            None,
        )
        if skill is None:
            continue
        try:
            loader = SkillLoader()
            loaded_skill = loader.load(skill)
            required_files = []
            for file_path in loaded_skill.required_files:
                try:
                    required_files.append(loader.load_required_file(loaded_skill, file_path))
                except SkillLoadError:
                    continue
            if required_files:
                loaded_skill = LoadedSkill(
                    skill=loaded_skill.skill,
                    content=loaded_skill.content,
                    required_files=loaded_skill.required_files,
                    required_file_contents=required_files,
                )
            loaded.append(loaded_skill)
        except SkillLoadError:
            continue
    return loaded
