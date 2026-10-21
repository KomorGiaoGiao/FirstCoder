"""技能相关 slash 命令。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firstcoder.app.commands import CommandResult
from firstcoder.skills.models import SkillCatalog, SkillDefinition


@dataclass(slots=True)
class SkillCommandHandler:
    """处理 `/skills`、`/skill <name>`、`/skill-use <name>` 与精确的 `/<skill-name> <instruction>`。

    catalog_provider 是惰性回调, 每次命令重新发现技能, 磁盘上新增技能不用重启。
    """

    catalog_provider: Callable[[], SkillCatalog]

    def handle(self, text: str) -> CommandResult:
        """按首个词精确匹配: /skills -> /skill -> /skill-use -> 精确技能名启动。

        前三个是固定命令; 最后兜底把 `/<name>` 当作技能启动尝试(命中时注入
        load_skill 指令走聊天路径)。都不匹配返回 handled=False 让位给后续
        handler —— 这也是"任意 slash 命令没被技能认领时还能报 Unknown command"
        的关键。
        """
        command = text.strip()
        if not command.startswith("/"):
            return CommandResult(handled=False)

        parts = command.split()
        name = parts[0]
        args = parts[1:]
        if name == "/skills":
            # 列表 + skill_picker action, TUI 原地打开选择器
            return self._list_skills()
        if name == "/skill":
            # 详情查看, _show_skill 内部做模糊匹配(name/path 精确或包含)
            return CommandResult(handled=True, output=self._show_skill(args))
        if name == "/skill-use":
            # 引用技能: skill_referenced action 让 TUI 把 load_skill 提示插进输入框
            return self._reference_skill(args)
        launched = self._launch_exact_skill(name, command)
        if launched is not None:
            return launched
        return CommandResult(handled=False)

    def _list_skills(self) -> CommandResult:
        catalog = self.catalog_provider().resolved()
        if not catalog.skills:
            return CommandResult(handled=True, output="No skills.")
        lines = ["Skills:"]
        for skill in catalog.skills:
            description = f" - {skill.description}" if skill.description else ""
            lines.append(f"- {skill.name} {skill.scope} {skill.path}{description}")
        return CommandResult(
            handled=True,
            output="\n".join(lines),
            action={
                "type": "skill_picker",
                "skills": [_skill_action_item(skill) for skill in catalog.skills],
                "selected_index": 0,
            },
        )

    def _show_skill(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /skill <name>"
        query = args[0].lower()
        catalog = self.catalog_provider().resolved()
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

    def _reference_skill(self, args: list[str]) -> CommandResult:
        if len(args) != 1:
            return CommandResult(handled=True, output="Usage: /skill-use <name>")
        query = args[0].lower()
        catalog = self.catalog_provider().resolved()
        skill = _find_skill(catalog.skills, query)
        if skill is None:
            return CommandResult(handled=True, output=f"Skill not found: {args[0]}")
        return CommandResult(
            handled=True,
            output=f"Referenced skill: {skill.name}",
            action={
                "type": "skill_referenced",
                "name": skill.name,
                "path": skill.path,
                "reference": f"请先调用 load_skill(name={skill.name}, args=<你的任务>)，再按照返回的指令继续。",
            },
        )

    def _launch_exact_skill(self, slash_name: str, command: str) -> CommandResult | None:
        """精确匹配 `/<skill-name>`: 命中时注入 load_skill 指令文本当作用户消息发出去。

        submit_chat action 让 TUI 把这段指令走正常聊天路径, 模型收到后调 load_skill
        工具拿到技能正文再执行。技能未命中返回 None, 交给后续 handler。
        """
        query = slash_name.removeprefix("/").lower()
        if not query:
            return None
        catalog = self.catalog_provider().resolved()
        skill = _find_exact_skill(catalog.skills, query)
        if skill is None:
            return None
        instruction = command[len(slash_name) :].strip()
        if not instruction:
            return CommandResult(handled=True, output=f"Usage: /{skill.name} <instruction>")
        return CommandResult(
            handled=True,
            output=f"Using skill: {skill.name}",
            action={
                "type": "submit_chat",
                "text": f"请先调用 load_skill(name={skill.name}, args={instruction})，再按照返回的指令继续。",
            },
        )


def _find_skill(skills: list[SkillDefinition], query: str) -> SkillDefinition | None:
    for skill in skills:
        if skill.name.lower() == query or skill.path.lower() == query:
            return skill
    for skill in skills:
        if query in skill.name.lower() or query in skill.path.lower():
            return skill
    return None


def _find_exact_skill(skills: list[SkillDefinition], query: str) -> SkillDefinition | None:
    for skill in skills:
        aliases = {skill.name.lower(), skill.path.lower(), _path_alias(skill.path)}
        if query in aliases:
            return skill
    return None


def _path_alias(path: str) -> str:
    value = path.lower()
    if value.endswith("/skill.md"):
        return value.rsplit("/", 2)[-2]
    if value.endswith(".md"):
        return value.rsplit("/", 1)[-1].removesuffix(".md")
    return value


def _skill_action_item(skill: SkillDefinition) -> dict[str, str]:
    return {
        "name": skill.name,
        "path": skill.path,
        "scope": skill.scope,
        "description": skill.description,
    }
