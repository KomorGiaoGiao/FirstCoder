"""Discover project-local and machine-global skills."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import yaml

from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillSource


def discover_project_skills(project_root: str | Path) -> SkillCatalog:
    root = Path(project_root)
    skills: list[SkillDefinition] = []
    index_content = ""

    project_skills = root / "skills"
    index_path = project_skills / "INDEX.md"
    if index_path.exists():
        index_content = _read_text(index_path)
    if project_skills.exists():
        skills.extend(
            _discover_markdown_skills(
                project_skills,
                root=root,
                source=SkillSource.PROJECT_MARKDOWN,
                skip_names={"INDEX.md"},
            )
        )

    project_agent_skills = root / ".agents" / "skills"
    if project_agent_skills.exists():
        skills.extend(
            _discover_agent_skills(
                project_agent_skills,
                root=root,
                source=SkillSource.PROJECT_AGENT_SKILL,
            )
        )

    return SkillCatalog(skills=_sort_and_dedupe(skills), index_content=index_content)


def discover_all_skills(project_root: str | Path, *, env: Mapping[str, str] | None = None) -> SkillCatalog:
    env = env or os.environ
    project_catalog = discover_project_skills(project_root)
    if _global_skills_disabled(env):
        return project_catalog

    global_skills: list[SkillDefinition] = []
    for root in _global_skill_roots(env):
        if not root.exists():
            continue
        global_skills.extend(_discover_global_root(root))

    return SkillCatalog(
        skills=_sort_and_dedupe([*project_catalog.skills, *global_skills]),
        index_content=project_catalog.index_content,
    )


def _discover_global_root(root: Path) -> list[SkillDefinition]:
    skills: list[SkillDefinition] = []
    skills.extend(
        _discover_agent_skills(
            root,
            root=root,
            source=SkillSource.GLOBAL_AGENT_SKILL,
        )
    )
    skills.extend(
        _discover_markdown_skills(
            root,
            root=root,
            source=SkillSource.GLOBAL_MARKDOWN,
            skip_names={"INDEX.md", "SKILL.md"},
        )
    )
    return skills


def _discover_markdown_skills(
    directory: Path,
    *,
    root: Path,
    source: SkillSource,
    skip_names: set[str],
) -> list[SkillDefinition]:
    skills: list[SkillDefinition] = []
    for path in sorted(directory.glob("*.md")):
        if path.name in skip_names:
            continue
        content = _read_text(path)
        metadata = _frontmatter_metadata(content)
        skills.append(
            SkillDefinition(
                name=_metadata_text(metadata, "name") or path.stem,
                path=_relative_path(path, root),
                source=source,
                root=str(root),
                description=_metadata_text(metadata, "description") or _first_heading(content) or _first_nonempty_line(content),
                triggers=_parse_triggers(metadata.get("triggers", "")),
            )
        )
    return skills


def _discover_agent_skills(directory: Path, *, root: Path, source: SkillSource) -> list[SkillDefinition]:
    skills: list[SkillDefinition] = []
    for path in sorted(directory.glob("*/SKILL.md")):
        content = _read_text(path)
        metadata = _frontmatter_metadata(content)
        skills.append(
            SkillDefinition(
                name=_metadata_text(metadata, "name") or path.parent.name,
                path=_relative_path(path, root),
                source=source,
                root=str(root),
                description=_metadata_text(metadata, "description") or _first_heading(content) or _first_nonempty_line(content),
                triggers=_parse_triggers(metadata.get("triggers", "")),
            )
        )
    return skills


def _global_skill_roots(env: Mapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME", str(Path.home())))
    roots = [
        home / ".agents" / "skills",
        home / ".codex" / "skills",
        home / ".firstcoder" / "skills",
    ]
    extra = env.get("FIRSTCODER_SKILL_ROOTS", "")
    for raw in extra.split(","):
        value = raw.strip()
        if value:
            roots.append(Path(value).expanduser())
    return roots


def _global_skills_disabled(env: Mapping[str, str]) -> bool:
    return env.get("FIRSTCODER_DISABLE_GLOBAL_SKILLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _sort_and_dedupe(skills: list[SkillDefinition]) -> list[SkillDefinition]:
    seen: set[tuple[str, str, str]] = set()
    result: list[SkillDefinition] = []
    for skill in sorted(skills, key=lambda item: (item.source.value, item.root, item.path)):
        key = (skill.source.value, skill.root, skill.path)
        if key in seen:
            continue
        seen.add(key)
        result.append(skill)
    return result


def _frontmatter_metadata(content: str) -> dict[str, object]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _parse_triggers(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(str(part).strip() for part in value if str(part).strip())
    if value is None:
        return ()
    return ()


def _metadata_text(metadata: Mapping[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) else ""


def _first_heading(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _first_nonempty_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
