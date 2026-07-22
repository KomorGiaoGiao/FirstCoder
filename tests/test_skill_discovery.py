from pathlib import Path

from firstcoder.skills.catalog import render_skill_catalog
from firstcoder.skills.discovery import discover_all_skills, discover_project_skills
from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillSource


def test_discovers_project_markdown_skills_and_uses_index_as_context(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "INDEX.md").write_text(
        "# Skill Index\n\n| Skill | 触发场景 |\n|---|---|\n| `daily-brief.md` | 今日资讯 |\n",
        encoding="utf-8",
    )
    (skills_dir / "daily-brief.md").write_text("# Daily Brief\n\n生成日报。", encoding="utf-8")

    catalog = discover_project_skills(tmp_path)

    assert catalog.index_content.startswith("# Skill Index")
    assert [skill.path for skill in catalog.skills] == ["skills/daily-brief.md"]
    skill = catalog.skills[0]
    assert skill.name == "daily-brief"
    assert skill.description == "Daily Brief"
    assert skill.source == SkillSource.PROJECT_MARKDOWN
    assert skill.root == str(tmp_path)


def test_discovers_project_agent_skill_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "fetch-tweet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fetch-tweet\ndescription: Fetch X/Twitter posts.\n---\n\n# Fetch Tweet\n",
        encoding="utf-8",
    )

    catalog = discover_project_skills(tmp_path)

    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "fetch-tweet"
    assert skill.description == "Fetch X/Twitter posts."
    assert skill.path == ".agents/skills/fetch-tweet/SKILL.md"
    assert skill.source == SkillSource.PROJECT_AGENT_SKILL


def test_discovers_quoted_name_and_folded_yaml_description(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "family-office"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: "family-office-research"\n'
        "description: >\n"
        "  Generate comprehensive family office research.\n"
        "  Use primary sources and verify claims.\n"
        "triggers:\n"
        "  - family office\n"
        "  - 家族办公室\n"
        "---\n\n"
        "# Family Office Research\n",
        encoding="utf-8",
    )

    skill = discover_project_skills(tmp_path).skills[0]

    assert skill.name == "family-office-research"
    assert skill.description == "Generate comprehensive family office research. Use primary sources and verify claims."
    assert skill.triggers == ("family office", "家族办公室")


def test_discovers_frontmatter_triggers(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "daily-brief.md").write_text(
        "---\n" "name: daily-brief\n" "description: Generate daily brief.\n" "triggers: 今日资讯, daily news\n" "---\n\n" "# Daily Brief\n",
        encoding="utf-8",
    )

    catalog = discover_project_skills(tmp_path)

    assert catalog.skills[0].triggers == ("今日资讯", "daily news")


def test_non_string_frontmatter_name_and_description_fall_back_safely(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname:\n  - invalid\ndescription:\n  nested: invalid\n---\n\n# Review safely\n",
        encoding="utf-8",
    )

    skill = discover_project_skills(tmp_path).skills[0]

    assert skill.name == "review"
    assert skill.description == "Review safely"


def test_discovers_global_agent_skills_from_default_roots(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".agents" / "skills" / "mail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mail\ndescription: Send and search email.\n---\n\n# Mail\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = discover_all_skills(tmp_path)

    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "mail"
    assert skill.source == SkillSource.GLOBAL_AGENT_SKILL
    assert skill.root == str(home / ".agents" / "skills")
    assert skill.path == "mail/SKILL.md"


def test_discovers_global_agent_skills_from_codex_default_root(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".codex" / "skills" / "imagegen"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: imagegen\ndescription: Generate images.\n---\n\n# ImageGen\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = discover_all_skills(tmp_path)

    assert [(skill.name, skill.source, skill.root, skill.path) for skill in catalog.skills] == [("imagegen", SkillSource.GLOBAL_AGENT_SKILL, str(home / ".codex" / "skills"), "imagegen/SKILL.md")]


def test_extra_global_skill_roots_and_disable_global_skills(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    extra_root = tmp_path / "extra-skills"
    extra_root.mkdir()
    (extra_root / "brief.md").write_text("# Brief Writer\n\n写简报。", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FIRSTCODER_SKILL_ROOTS", str(extra_root))

    enabled = discover_all_skills(tmp_path)

    assert [(skill.name, skill.source) for skill in enabled.skills] == [("brief", SkillSource.GLOBAL_MARKDOWN)]

    monkeypatch.setenv("FIRSTCODER_DISABLE_GLOBAL_SKILLS", "1")
    disabled = discover_all_skills(tmp_path)

    assert disabled.skills == []


def test_catalog_fingerprint_changes_when_skill_metadata_changes(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "review.md"
    skill_path.write_text("# Review\n\n初版。", encoding="utf-8")

    before = discover_project_skills(tmp_path).fingerprint
    skill_path.write_text("# Review Updated\n\n新版。", encoding="utf-8")
    after = discover_project_skills(tmp_path).fingerprint

    assert before != after


def test_resolved_catalog_prefers_project_skill_for_duplicate_name() -> None:
    global_skill = SkillDefinition(
        name="review",
        path="review/SKILL.md",
        source=SkillSource.GLOBAL_AGENT_SKILL,
        root="/global",
        description="Global review rules.",
    )
    project_skill = SkillDefinition(
        name="review",
        path=".agents/skills/review/SKILL.md",
        source=SkillSource.PROJECT_AGENT_SKILL,
        root="/project",
        description="Project review rules.",
    )

    resolved = SkillCatalog(skills=[global_skill, project_skill]).resolved()

    assert resolved.skills == [project_skill]


def test_render_skill_catalog_hides_filesystem_metadata_and_bounds_whole_lines() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}",
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL_AGENT_SKILL,
            root="/Users/example/.agents/skills",
            description=("A long description with\nextra whitespace. " * 20),
        )
        for index in range(100)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))

    assert len(rendered) <= 8_000
    assert "root=" not in rendered
    assert "SKILL.md" not in rendered
    assert "global_agent_skill" not in rendered
    assert "\nextra whitespace" not in rendered
    assert rendered.splitlines()[0].startswith("- skill-000: A long description")
    assert rendered.splitlines()[-1] == "Use load_skill(name, args?) to load full instructions when needed."
    assert all(line.startswith("- skill-") for line in rendered.splitlines()[:-1])
