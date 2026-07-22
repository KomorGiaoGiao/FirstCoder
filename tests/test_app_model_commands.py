from dataclasses import dataclass

import pytest

from firstcoder.app.model_commands import ModelCommandHandler, ModelState


@dataclass
class FakeSwitcher:
    current: ModelState = ModelState(provider="fake", model="old-model")
    choices: list[ModelState] | None = None
    switched_spec: str | None = None
    error: ValueError | None = None

    def current_model(self) -> ModelState:
        return self.current

    def model_choices(self) -> list[ModelState]:
        return self.choices or [self.current, ModelState(provider="fake", model="new-model")]

    def switch_model(self, spec: str) -> ModelState:
        self.switched_spec = spec
        if self.error is not None:
            raise self.error
        self.current = ModelState(provider="fake", model="new-model")
        return self.current


def test_model_command_shows_current_model() -> None:
    result = ModelCommandHandler(FakeSwitcher()).handle("/model")

    assert result.handled is True
    assert "Current model: fake/old-model" in result.output
    assert "Select a model:" in result.output
    assert result.action == {
        "type": "model_picker",
        "models": [
            {"provider": "fake", "model": "old-model"},
            {"provider": "fake", "model": "new-model"},
        ],
        "selected_index": 0,
    }


def test_models_command_is_an_alias_for_model_picker() -> None:
    switcher = FakeSwitcher(
        choices=[
            ModelState(provider="yuren", model="gpt-main"),
            ModelState(provider="mimo", model="mimo-v2.5-pro"),
        ]
    )

    result = ModelCommandHandler(switcher).handle("/models")

    assert result.handled is True
    assert result.action == {
        "type": "model_picker",
        "models": [
            {"provider": "yuren", "model": "gpt-main"},
            {"provider": "mimo", "model": "mimo-v2.5-pro"},
        ],
        "selected_index": 0,
    }


def test_models_command_does_not_accept_a_model_argument() -> None:
    result = ModelCommandHandler(FakeSwitcher()).handle("/models fake/new")

    assert result.handled is False


def test_model_command_switches_model() -> None:
    switcher = FakeSwitcher()

    result = ModelCommandHandler(switcher).handle("/model new-model")

    assert result.handled is True
    assert switcher.switched_spec == "new-model"
    assert result.output == "Model switched: fake/new-model"
    assert result.action == {"type": "model_changed", "provider": "fake", "model": "new-model"}


def test_model_command_reports_switch_errors() -> None:
    result = ModelCommandHandler(FakeSwitcher(error=ValueError("bad model"))).handle("/model bad")

    assert result.handled is True
    assert result.output == "Model switch failed: bad model"
    assert result.action is None


def test_model_command_reports_unknown_catalog_model() -> None:
    result = ModelCommandHandler(FakeSwitcher(error=ValueError("未配置模型：missing/model"))).handle("/model missing/model")

    assert result.handled is True
    assert result.output == "Model switch failed: 未配置模型：missing/model"
    assert result.action is None


@pytest.mark.parametrize("text", ["/mode", "hello"])
def test_model_command_ignores_other_input(text: str) -> None:
    assert ModelCommandHandler(FakeSwitcher()).handle(text).handled is False
