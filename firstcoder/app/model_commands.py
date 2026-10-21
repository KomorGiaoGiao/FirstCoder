"""模型切换 slash 命令。

/model 无参打开选择器(model_picker action), /model <provider>/<model> 直接切换
(model_changed action 通知 TUI 刷顶栏)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult


@dataclass(frozen=True, slots=True)
class ModelState:
    provider: str
    model: str


class ModelSwitcherLike(Protocol):
    def current_model(self) -> ModelState: ...

    def model_choices(self) -> list[ModelState]: ...

    def switch_model(self, spec: str) -> ModelState: ...


@dataclass(slots=True)
class ModelCommandHandler:
    """处理 `/model` 的 provider/model 快捷切换。"""

    switcher: ModelSwitcherLike

    def handle(self, text: str) -> CommandResult:
        """按空白归一化后的整串匹配: /model|/models 打开选择器, /model <spec> 直接切换。

        handled=False 只出现在两种"不归我管"的情况: 不是 slash 输入, 或不是
        /model 开头, 让位给后续 handler。切换失败(模型未配置等) 返回
        handled=True 并把错误当输出展示, 不再让别的 handler 试。
        """
        command = " ".join(text.strip().split())
        if not command.startswith("/"):
            return CommandResult(handled=False)
        if command in {"/model", "/models"}:
            # 无参: action 携带候选列表与当前项索引, TUI 原地渲染 model picker
            current = self.switcher.current_model()
            choices = self.switcher.model_choices()
            return CommandResult(
                handled=True,
                output=_render_model_picker(current, choices),
                action={
                    "type": "model_picker",
                    "models": [_model_state_dict(choice) for choice in choices],
                    "selected_index": _current_model_index(current, choices),
                },
            )
        if not command.startswith("/model "):
            return CommandResult(handled=False)

        spec = command.removeprefix("/model ").strip()
        if not spec:
            return CommandResult(handled=True, output="Usage: /model <model> or /model <provider>/<model>")
        try:
            switched = self.switcher.switch_model(spec)
        except ValueError as error:
            return CommandResult(handled=True, output=f"Model switch failed: {error}")

        return CommandResult(
            handled=True,
            output=f"Model switched: {switched.provider}/{switched.model}",
            action={
                # TUI 收到后刷新顶栏的 provider/model 显示
                "type": "model_changed",
                "provider": switched.provider,
                "model": switched.model,
            },
        )


def _render_model_picker(current: ModelState, choices: list[ModelState]) -> str:
    lines = [f"Current model: {current.provider}/{current.model}", "Select a model:"]
    for index, choice in enumerate(choices):
        marker = ">" if choice == current else " "
        lines.append(f"{marker} {index + 1}. {choice.provider}/{choice.model}")
    lines.append("Use up/down and enter to switch, or type /model <provider>/<model>.")
    return "\n".join(lines)


def _current_model_index(current: ModelState, choices: list[ModelState]) -> int:
    try:
        return choices.index(current)
    except ValueError:
        return 0


def _model_state_dict(state: ModelState) -> dict[str, str]:
    return {"provider": state.provider, "model": state.model}
