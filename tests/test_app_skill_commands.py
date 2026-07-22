from pathlib import Path

from firstcoder.app.skill_commands import SkillCommandHandler
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.skills.models import SkillCatalog


def test_skills_command_lists_discovered_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text(
        "---\nname: brief\ndescription: Write a brief.\ntriggers: news, summary\n---\n\n# Brief\n",
        encoding="utf-8",
    )
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/skills")

    assert result.handled is True
    assert "Skills:" in result.output
    assert "- brief project skills/brief.md" in result.output
    assert "Write a brief." in result.output
    assert result.action == {
        "type": "skill_picker",
        "skills": [
            {
                "name": "brief",
                "path": "skills/brief.md",
                "scope": "project",
                "description": "Write a brief.",
            }
        ],
        "selected_index": 0,
    }


def test_skill_command_shows_single_skill_details(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / ".agents" / "skills" / "fetch-tweet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fetch-tweet\ndescription: Fetch tweet content.\ntriggers: x.com, twitter\n---\n\n# Fetch Tweet\n",
        encoding="utf-8",
    )
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/skill fetch-tweet")

    assert result.handled is True
    assert "Skill: fetch-tweet" in result.output
    assert "Scope: project" in result.output
    assert "Source: project_agent_skill" in result.output
    assert "Path: .agents/skills/fetch-tweet/SKILL.md" in result.output
    assert "Triggers: x.com, twitter" in result.output


def test_skill_command_reports_missing_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/skill missing")

    assert result.handled is True
    assert result.output == "Skill not found: missing"


def test_skill_use_command_references_skill_for_input(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/skill-use brief")

    assert result.handled is True
    assert result.output == "Referenced skill: brief"
    assert result.action == {
        "type": "skill_referenced",
        "name": "brief",
        "path": "skills/brief.md",
        "reference": "请先调用 load_skill(name=brief, args=<你的任务>)，再按照返回的指令继续。",
    }


def test_skill_use_command_requires_one_skill_name() -> None:
    handler = SkillCommandHandler(catalog_provider=SkillCatalog)

    result = handler.handle("/skill-use")

    assert result.handled is True
    assert result.output == "Usage: /skill-use <name>"


def test_exact_skill_slash_command_submits_instruction_to_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / ".agents" / "skills" / "fetch-tweet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fetch-tweet\ndescription: Fetch tweet content.\n---\n\n# Fetch Tweet\n",
        encoding="utf-8",
    )
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/fetch-tweet 读取 https://x.com/a/status/1")

    assert result.handled is True
    assert result.output == "Using skill: fetch-tweet"
    assert result.action == {
        "type": "submit_chat",
        "text": "请先调用 load_skill(name=fetch-tweet, args=读取 https://x.com/a/status/1)，再按照返回的指令继续。",
    }


def test_exact_skill_slash_command_requires_instruction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/brief")

    assert result.handled is True
    assert result.output == "Usage: /brief <instruction>"


def test_exact_skill_slash_command_does_not_use_substring_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    handler = SkillCommandHandler(catalog_provider=lambda: discover_all_skills(tmp_path))

    result = handler.handle("/bri 写日报")

    assert result.handled is False
