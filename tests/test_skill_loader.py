from pathlib import Path

import pytest

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.skills.loader import SkillLoadError, SkillLoader
from firstcoder.skills.models import SkillDefinition, SkillSource
from firstcoder.skills.session import append_skill_loaded, append_skill_required_file_loaded


def test_skill_loader_reads_complete_skill_and_required_files(tmp_path: Path) -> None:
    skill_path = tmp_path / "skills" / "brief.md"
    skill_path.parent.mkdir()
    skill_path.write_text(
        "# Brief\n\n开始前必须读取：\n\n1. `AGENTS.md`\n2. `docs/evidence-policy.md`\n",
        encoding="utf-8",
    )
    skill = SkillDefinition(
        name="brief",
        path="skills/brief.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
        description="简报",
    )

    loaded = SkillLoader().load(skill)

    assert loaded.skill == skill
    assert loaded.content == skill_path.read_text(encoding="utf-8")
    assert loaded.bytes == len(loaded.content.encode("utf-8"))
    assert loaded.content_hash
    assert loaded.required_files == ["AGENTS.md", "docs/evidence-policy.md"]


def test_skill_loader_extracts_required_file_from_marker_line(tmp_path: Path) -> None:
    skill = SkillDefinition(
        name="brief",
        path="skills/brief.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
        description="简报",
    )

    loaded = SkillLoader().load_from_content(
        skill,
        "# Brief\n\n开始前必须读取 `docs/evidence-policy.md`。\n",
    )

    assert loaded.required_files == ["docs/evidence-policy.md"]


def test_skill_loader_extracts_required_files_from_common_chinese_headings(tmp_path: Path) -> None:
    skill = SkillDefinition(
        name="claim-review",
        path="skills/claim-review.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
        description="复核",
    )

    loaded = SkillLoader().load_from_content(
        skill,
        ("# Claim Review\n\n" "## 预读文件\n\n" "- `docs/evidence-policy.md`\n" "- `skills/litigation-review.md`\n\n" "## 执行\n\n" "必须先读：\n\n" "- `skills/source-verification.md`\n"),
    )

    assert loaded.required_files == [
        "docs/evidence-policy.md",
        "skills/litigation-review.md",
    ]


def test_skill_loader_rejects_missing_or_escaping_path(tmp_path: Path) -> None:
    loader = SkillLoader()
    missing = SkillDefinition(
        name="missing",
        path="skills/missing.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
    )
    escaping = SkillDefinition(
        name="escape",
        path="../secret.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
    )

    with pytest.raises(SkillLoadError):
        loader.load(missing)
    with pytest.raises(SkillLoadError):
        loader.load(escaping)


def test_skill_loader_reads_required_file_safely(tmp_path: Path) -> None:
    skill_path = tmp_path / "skills" / "brief.md"
    required_path = tmp_path / "docs" / "evidence-policy.md"
    skill_path.parent.mkdir()
    required_path.parent.mkdir()
    skill_path.write_text("# Brief\n\n必须读取 `docs/evidence-policy.md`。\n", encoding="utf-8")
    required_path.write_text("# Evidence Policy\n\nUse primary sources.\n", encoding="utf-8")
    skill = SkillDefinition(
        name="brief",
        path="skills/brief.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
    )
    loaded = SkillLoader().load(skill)

    required = SkillLoader().load_required_file(loaded, "docs/evidence-policy.md")

    assert required.file_path == "docs/evidence-policy.md"
    assert required.content == "# Evidence Policy\n\nUse primary sources.\n"
    assert required.content_hash


def test_skill_loader_rejects_missing_or_escaping_required_file(tmp_path: Path) -> None:
    skill = SkillDefinition(
        name="brief",
        path="skills/brief.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
    )
    loaded = SkillLoader().load_from_content(skill, "# Brief\n")
    loader = SkillLoader()

    with pytest.raises(SkillLoadError):
        loader.load_required_file(loaded, "missing.md")
    with pytest.raises(SkillLoadError):
        loader.load_required_file(loaded, "../secret.md")


def test_append_skill_loaded_writes_auditable_session_event(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_skill")
    skill = SkillDefinition(
        name="fetch-tweet",
        path="fetch-tweet/SKILL.md",
        source=SkillSource.GLOBAL_AGENT_SKILL,
        root="/Users/x/.agents/skills",
        description="Fetch tweets.",
    )
    loaded = SkillLoader().load_from_content(skill, "# Fetch Tweet\n")

    append_skill_loaded(writer, loaded)

    events = store.list_events("sess_skill")
    assert len(events) == 1
    event = events[0]
    assert event.type == "skill_loaded"
    assert event.payload["skill_name"] == "fetch-tweet"
    assert event.payload["skill_scope"] == "global"
    assert event.payload["skill_root"] == "/Users/x/.agents/skills"
    assert event.payload["skill_path"] == "fetch-tweet/SKILL.md"
    assert event.payload["content_hash"] == loaded.content_hash
    assert event.payload["bytes"] == loaded.bytes


def test_append_skill_required_file_loaded_writes_auditable_session_event(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_skill")
    skill = SkillDefinition(
        name="brief",
        path="skills/brief.md",
        source=SkillSource.PROJECT_MARKDOWN,
        root=str(tmp_path),
    )
    loaded = SkillLoader().load_from_content(skill, "# Brief\n")
    required = SkillLoader().load_required_file_from_content(
        loaded,
        "docs/evidence-policy.md",
        "# Evidence Policy\n",
    )

    append_skill_required_file_loaded(writer, required)

    events = store.list_events("sess_skill")
    assert len(events) == 1
    event = events[0]
    assert event.type == "skill_required_file_loaded"
    assert event.payload["skill_path"] == "skills/brief.md"
    assert event.payload["file_path"] == "docs/evidence-policy.md"
    assert event.payload["content_hash"] == required.content_hash
    assert event.payload["bytes"] == required.bytes
