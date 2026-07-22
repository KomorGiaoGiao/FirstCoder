"""Deterministic skill routing."""

from __future__ import annotations

import re

from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillRoutingDecision, SkillSource


class SkillRouter:
    """Route a user message to a likely skill without calling a model."""

    def route(self, user_message: str, *, agents_md: str, catalog: SkillCatalog) -> SkillRoutingDecision:
        explicit = _explicit_matches(user_message, catalog.skills)
        if explicit:
            return SkillRoutingDecision(
                selected=_preferred_skill(explicit),
                candidates=explicit,
                reason="explicit",
                confidence="high",
            )

        agents_match = _agents_route_matches(user_message, agents_md, catalog.skills)
        if agents_match:
            return SkillRoutingDecision(
                selected=agents_match[0],
                candidates=agents_match,
                reason="agents_route",
                confidence="high",
            )

        metadata = _metadata_matches(user_message, catalog.skills)
        if len(metadata) == 1:
            return SkillRoutingDecision(
                selected=metadata[0],
                candidates=metadata,
                reason="metadata_match",
                confidence="high",
            )
        if len(metadata) > 1:
            same_name = {skill.name for skill in metadata}
            if len(same_name) == 1:
                selected = _preferred_skill(metadata)
                return SkillRoutingDecision(
                    selected=selected,
                    candidates=metadata,
                    reason="metadata_match",
                    confidence="high",
                )
            return SkillRoutingDecision(
                selected=None,
                candidates=metadata,
                reason="ambiguous",
                confidence="medium",
            )
        return SkillRoutingDecision(selected=None, candidates=[], reason="none", confidence="none")


def _explicit_matches(user_message: str, skills: list[SkillDefinition]) -> list[SkillDefinition]:
    normalized = user_message.lower()
    return [skill for skill in skills if skill.name.lower() in normalized or skill.path.lower() in normalized]


def _agents_route_matches(user_message: str, agents_md: str, skills: list[SkillDefinition]) -> list[SkillDefinition]:
    if not agents_md.strip():
        return []
    normalized_message = _normalize_text(user_message)
    matches: list[tuple[int, int, SkillDefinition]] = []
    for line_number, line in enumerate(agents_md.splitlines()):
        line_skills = [(line.index(skill.path), skill) for skill in skills if skill.path in line]
        if not line_skills:
            continue
        for _, skill in line_skills:
            route_text = line.replace(skill.path, " ")
            if _has_meaningful_overlap(normalized_message, _normalize_text(route_text)):
                matches.append((line_number, line.index(skill.path), skill))
    return [skill for _, _, skill in sorted(matches, key=lambda item: (item[0], item[1]))]


def _metadata_matches(user_message: str, skills: list[SkillDefinition]) -> list[SkillDefinition]:
    normalized_message = _normalize_text(user_message)
    scored: list[tuple[int, SkillDefinition]] = []
    for skill in skills:
        haystack = _normalize_text(" ".join([skill.name, skill.description, *skill.triggers]))
        score = _overlap_score(normalized_message, haystack)
        if score > 0:
            scored.append((score, skill))
    if not scored:
        return []
    max_score = max(score for score, _ in scored)
    return _sort_by_preference([skill for score, skill in scored if score == max_score])


def _preferred_skill(skills: list[SkillDefinition]) -> SkillDefinition:
    return _sort_by_preference(skills)[0]


def _sort_by_preference(skills: list[SkillDefinition]) -> list[SkillDefinition]:
    return sorted(skills, key=lambda skill: (_source_priority(skill.source), skill.path))


def _source_priority(source: SkillSource) -> int:
    if source == SkillSource.PROJECT_AGENT_SKILL:
        return 0
    if source == SkillSource.PROJECT_MARKDOWN:
        return 1
    if source == SkillSource.GLOBAL_AGENT_SKILL:
        return 2
    return 3


def _has_meaningful_overlap(message: str, route_text: str) -> bool:
    return _overlap_score(message, route_text) >= 2


def _overlap_score(left: str, right: str) -> int:
    left_tokens = set(_tokens(_expand_aliases(left)))
    right_tokens = set(_tokens(_expand_aliases(right)))
    return len(left_tokens & right_tokens)


def _normalize_text(value: str) -> str:
    return value.lower()


def _tokens(value: str) -> list[str]:
    tokens = [token for token in re.split(r"[^\w\u4e00-\u9fff]+", value) if len(token) >= 2]
    for chinese in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        tokens.extend(_char_ngrams(chinese, size=2))
        tokens.extend(_char_ngrams(chinese, size=3))
    return tokens


def _char_ngrams(value: str, *, size: int) -> list[str]:
    if len(value) < size:
        return []
    return [value[index : index + size] for index in range(len(value) - size + 1)]


def _expand_aliases(value: str) -> str:
    expanded = value
    lowered = value.lower()
    if "x.com" in lowered or "twitter" in lowered or "tweet" in lowered:
        expanded += " x twitter tweet post 帖子 推文"
    if "家办" in value:
        expanded += " 家族办公室"
    return expanded
