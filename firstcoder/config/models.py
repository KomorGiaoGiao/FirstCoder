"""多模型 Catalog 配置模型与 TOML 解析。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping


class ModelCatalogError(ValueError):
    """模型目录配置缺失或不合法。"""


_RESERVED_REQUEST_EXTRA_BODY_FIELDS = {
    "model",
    "messages",
    "input",
    "tools",
    "tool_choice",
    "stream",
    "temperature",
    "max_tokens",
    "max_completion_tokens",
}


@dataclass(frozen=True, slots=True)
class ModelRequestOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra_body", deepcopy(dict(self.extra_body)))


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    id: str
    type: str
    base_url: str | None = None
    api_key_env: str | None = None
    parallel_tool_calls: bool | None = None
    streaming: bool | None = None


@dataclass(frozen=True, slots=True)
class ModelProfile:
    ref: str
    provider_id: str
    model_id: str
    label: str
    provider: ProviderProfile
    request: ModelRequestOptions


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    default_ref: str | None
    profiles: tuple[ModelProfile, ...]

    def list(self) -> list[ModelProfile]:
        return list(self.profiles)

    def get(self, ref: str) -> ModelProfile | None:
        return next((profile for profile in self.profiles if profile.ref == ref), None)

    def require(self, ref: str) -> ModelProfile:
        profile = self.get(ref)
        if profile is None:
            raise ModelCatalogError(f"未配置模型：{ref}")
        return profile


def _deep_merge_dicts(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if not config or name not in config:
        return {}
    value = config[name]
    if not isinstance(value, dict):
        raise ModelCatalogError(f"[{name}] 配置必须是表")
    return value


def _merged_sections(global_config: Mapping[str, Any] | None, project_config: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    return _deep_merge_dicts(_section(global_config, name), _section(project_config, name))


def _optional_str(value: Any, field_name: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelCatalogError(f"{field_name} 必须是非空字符串")
    return value


def _provider_profile(provider_id: str, raw: Any) -> ProviderProfile:
    if not isinstance(raw, dict):
        raise ModelCatalogError(f"provider {provider_id} 必须是表")
    provider_type = _optional_str(raw.get("type"), f"provider {provider_id}.type", allow_none=False)
    return ProviderProfile(
        id=provider_id,
        type=provider_type or "",
        base_url=_optional_str(raw.get("base_url"), f"provider {provider_id}.base_url"),
        api_key_env=_optional_str(raw.get("api_key_env"), f"provider {provider_id}.api_key_env"),
        parallel_tool_calls=_optional_bool(raw.get("parallel_tool_calls"), f"provider {provider_id}.parallel_tool_calls"),
        streaming=_optional_bool(raw.get("streaming"), f"provider {provider_id}.streaming"),
    )


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ModelCatalogError(f"{field_name} 必须是布尔值")
    return value


def _request_options(raw: Any, *, ref: str) -> ModelRequestOptions:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ModelCatalogError(f"模型 {ref}.request 必须是表")
    temperature = raw.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))):
        raise ModelCatalogError(f"模型 {ref}.temperature 必须是数字")
    max_tokens = raw.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0):
        raise ModelCatalogError(f"模型 {ref}.max_tokens 必须是大于 0 的整数")
    reasoning_effort = _optional_str(raw.get("reasoning_effort"), f"模型 {ref}.reasoning_effort")
    extra = raw.get("extra_body", {})
    if not isinstance(extra, dict):
        raise ModelCatalogError(f"模型 {ref}.extra_body 必须是表")
    forbidden = _RESERVED_REQUEST_EXTRA_BODY_FIELDS.intersection(extra)
    if forbidden:
        raise ModelCatalogError(f"模型 {ref}.extra_body 不得覆盖：{', '.join(sorted(forbidden))}")
    extra_copy = deepcopy(extra)
    if reasoning_effort is not None:
        if "reasoning_effort" in extra_copy:
            raise ModelCatalogError(f"模型 {ref}.reasoning_effort 与 extra_body 冲突")
        extra_copy["reasoning_effort"] = reasoning_effort
    return ModelRequestOptions(temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort, extra_body=extra_copy)


def build_model_catalog(
    global_config: Mapping[str, Any] | None = None,
    project_config: Mapping[str, Any] | None = None,
) -> ModelCatalog:
    """合并全局/项目 TOML，并构造不可变模型目录。"""
    providers_raw = _merged_sections(global_config, project_config, "providers")
    models_raw = _merged_sections(global_config, project_config, "models")
    if not models_raw:
        if any(config and ("model" in config or "provider" in config) for config in (global_config, project_config)):
            raise ModelCatalogError("旧的 model + [provider] 配置已不受支持；请迁移到 default_model + [providers] + [models]")
        return ModelCatalog(default_ref=None, profiles=())

    providers = {provider_id: _provider_profile(provider_id, raw) for provider_id, raw in providers_raw.items()}
    profiles: list[ModelProfile] = []
    for ref, raw in sorted(models_raw.items()):
        if not isinstance(ref, str) or "/" not in ref:
            raise ModelCatalogError(f"模型引用无效：{ref}")
        provider_id, model_id = ref.split("/", 1)
        if not provider_id or not model_id:
            raise ModelCatalogError(f"模型引用无效：{ref}")
        provider = providers.get(provider_id)
        if provider is None:
            raise ModelCatalogError(f"模型 {ref} 指向的 provider {provider_id} 缺失")
        if not isinstance(raw, dict):
            raise ModelCatalogError(f"模型 {ref} 必须是表")
        label = raw.get("label", ref)
        if not isinstance(label, str):
            raise ModelCatalogError(f"模型 {ref}.label 必须是字符串")
        profiles.append(ModelProfile(ref, provider_id, model_id, label, provider, _request_options(raw.get("request"), ref=ref)))
    default_ref = _config_value(project_config, "default_model") or _config_value(global_config, "default_model")
    if default_ref is not None and default_ref not in {profile.ref for profile in profiles}:
        raise ModelCatalogError(f"默认模型未配置：{default_ref}")
    return ModelCatalog(default_ref=default_ref, profiles=tuple(profiles))


def _config_value(config: Mapping[str, Any] | None, name: str) -> str | None:
    value = config.get(name) if config else None
    return value if isinstance(value, str) and value else None


__all__ = [
    "ModelCatalog",
    "ModelCatalogError",
    "ModelProfile",
    "ModelRequestOptions",
    "ProviderProfile",
    "build_model_catalog",
]
