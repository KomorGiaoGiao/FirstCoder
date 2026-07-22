"""Model switching slash command."""

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
    """Handle `/model` commands for quick provider/model switching."""

    switcher: ModelSwitcherLike

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if not command.startswith("/"):
            return CommandResult(handled=False)
        if command in {"/model", "/models"}:
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
