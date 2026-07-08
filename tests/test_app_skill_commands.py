from pathlib import Path

from firstcoder.app.skill_commands import SkillCommandHandler
from firstcoder.skills.discovery import discover_all_skills


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
