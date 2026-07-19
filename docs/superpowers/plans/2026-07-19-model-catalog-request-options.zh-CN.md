# 多模型目录与请求参数实现计划

> **给执行型 Agent：** 必须逐项使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 实施本计划。所有步骤均使用复选框跟踪。

**目标：** 让 FirstCoder 能在 TOML 中持久保存多个模型及其 Provider/请求参数，并通过 TUI `/models` 选择器或 `/model <provider>/<model>` 命令安全切换；主 Agent 请求可传递 `temperature`、`max_tokens`、`reasoning_effort` 和其他白名单外的扩展请求参数。

**架构：** 保留现有 OpenAI Chat Completions 和 Anthropic Messages 两条 Provider 路径，不接入也不替换为 Responses API。配置层把全局和项目 TOML 中的 `providers`、`models` 深度合并成不可变 ModelCatalog；运行时只根据当前选中的 ModelProfile 重建 Provider，并把该 Profile 的 RequestOptions 仅注入主 Agent 的同步和流式请求。TUI 当前选择和最近十个有效模型写入 `<project>/.firstcoder/model_state.json`，模型定义及密钥引用始终留在 TOML 中。

**技术栈：** Python 3.11+、`tomllib`、dataclasses、OpenAI Python SDK、Anthropic SDK、Textual、pytest。

---

## 范围和边界

- 本期只实现 FirstCoder 自己调用的 Chat Completions-compatible / Anthropic Messages 请求；不实现 `client.responses.create()`、`previous_response_id` 或 OpenAI 托管工具。
- `reasoning_effort` 是 OpenAI-compatible 请求的便捷字段，最终作为顶层请求扩展参数透传；任何中转站是否接受该字段仍由该中转站决定。
- `extra_body` 用于厂商私有参数。配置解析拒绝其覆盖模型、消息、工具、流式和 token 上限等 FirstCoder 控制字段。
- 同一模型的多个 effort 档位（例如 `low`/`high` variant）不在本期实现。第一版一个 ModelProfile 对应一套稳定请求参数；后续可以在不改变 Catalog 的前提下新增 Variant 层。
- `/model <不带 provider 的模型名>` 保留为兼容快捷方式：复用当前 Provider 和当前 RequestOptions 临时换模型名，不写入模型目录或状态文件。跨 Provider 的切换必须命中显式配置的 ModelProfile。
- 模型选择是项目级运行时偏好，不写回 `firstcoder.toml`，也不绑定到历史 session；恢复旧 session 时使用当前有效选择。

## 目标配置格式

```toml
# 仅在不存在 --model 时作为启动默认值；可省略。
default_model = "yuren/gpt-5.6-terra"

[providers.yuren]
type = "openai-compatible"
base_url = "https://yurenapi.cn/v1"
api_key_env = "YURENAPI_API_KEY"
parallel_tool_calls = true

[providers.mimo]
type = "openai-compatible"
base_url = "https://token-plan-cn.xiaomimimo.com/v1"
api_key_env = "MIMO_API_KEY"

[models."yuren/gpt-5.6-terra"]
label = "Yuren Terra"

[models."yuren/gpt-5.6-terra".request]
temperature = 0.2
max_tokens = 8192
reasoning_effort = "high"
extra_body = { reasoning_summary = "auto" }

[models."mimo/mimo-v2.5-pro"]
label = "MiMo Pro"

[models."mimo/mimo-v2.5-pro".request]
max_tokens = 8192
reasoning_effort = "medium"
```

模型表键总是 `<provider-id>/<真实模型-id>`；模型 ID 内允许再带 `/`，解析时只按第一个 `/` 分割。`providers.<provider-id>` 的键是 TUI 与命令中显示和选择的 Provider ID，`type` 是 `openai-compatible`、`anthropic` 或现有 preset 名称。

## 配置优先级和启动选择顺序

```text
配置字段：环境变量/.env（仅覆盖现有厂商环境变量）
          > 项目 firstcoder.toml
          > 全局 ~/.config/firstcoder/config.toml

模型目录：全局 providers/models 深度合并项目 providers/models
          同名标量、request 字段和 extra_body 字段由项目覆盖

启动选择：--model
          > default_model
          > model_state.json 的 last_selected（若仍在 Catalog）
          > Catalog 第一个模型
          > 旧单 Provider 配置生成的兼容模型
```

## 文件变更总览

| 文件 | 变更职责 |
| --- | --- |
| `firstcoder/config/models.py`（新增） | 定义 ModelProfile、ModelCatalog、TOML 合并、校验和旧配置适配。 |
| `firstcoder/config/settings.py` | 向 AppConfig 暴露 ModelCatalog，不改变现有 MCP 合并语义。 |
| `firstcoder/providers/types.py` | 增加不可变的主请求选项对象。 |
| `firstcoder/providers/factory.py` | 根据显式 ModelProfile 构造 Provider；保留旧 factory 行为。 |
| `firstcoder/agent/loop.py` | 只在主 Agent 的同步/流式请求注入 RequestOptions。 |
| `firstcoder/app/runtime.py` | 持有当前 RequestOptions，并传入每次新建的 AgentLoop。 |
| `firstcoder/app/model_state.py`（新增） | 原子保存项目的 last selected 和 recent 模型引用。 |
| `firstcoder/app/factory.py` | 解析启动选择、创建/切换完整 Profile，并同步 summarizer。 |
| `firstcoder/app/model_commands.py` | 支持 `/models` 别名和 Catalog 驱动的选择器。 |
| `firstcoder/cli.py` | 添加 `--model`，并让 `config show` 可展示默认值和目录。 |
| `tests/test_config.py` | 覆盖 Catalog、深度合并、错误和旧配置兼容。 |
| `tests/test_providers.py` | 覆盖 `reasoning_effort` 和扩展字段在现有 provider 中的透传。 |
| `tests/test_model_request_options.py`（新增） | 覆盖主循环同步/流式注入，确保内部请求不受污染。 |
| `tests/test_model_state.py`（新增） | 覆盖模型选择状态的原子 round-trip 和失效回退。 |
| `tests/test_app_factory.py`、`tests/test_app_model_commands.py`、`tests/test_cli.py` | 覆盖启动、TUI/命令切换和 CLI 覆盖。 |
| `README.md`、`README.zh-CN.md` | 给用户提供配置和命令说明。 |

### Task 1：先用测试锁定多模型 TOML 的解析与合并

**文件：**

- 新增：`firstcoder/config/models.py`
- 修改：`firstcoder/config/settings.py:22-124`
- 修改：`firstcoder/config/__init__.py`
- 修改：`tests/test_config.py`

- [ ] **步骤 1：编写会失败的 Catalog 测试**

在 `tests/test_config.py` 新增以下 fixture 和测试。fixture 同时覆盖同名 Provider 的项目级覆盖、同名 Model 的 request/extra_body 深度覆盖和另一个全局模型的保留。

```python
from firstcoder.config.models import ModelCatalogError


def test_model_catalog_deep_merges_global_and_project_entries() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={},
        global_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://global.example/v1",
                    "api_key_env": "YUREN_API_KEY",
                }
            },
            "models": {
                "yuren/gpt-main": {
                    "label": "Global label",
                    "request": {
                        "temperature": 0.2,
                        "extra_body": {"reasoning_effort": "medium", "reasoning_summary": "auto"},
                    },
                },
                "yuren/gpt-cheap": {},
            },
        },
        project_config={
            "default_model": "yuren/gpt-main",
            "providers": {"yuren": {"base_url": "https://project.example/v1"}},
            "models": {
                "yuren/gpt-main": {
                    "label": "Project label",
                    "request": {"max_tokens": 8192, "extra_body": {"reasoning_effort": "high"}},
                }
            },
        },
    )

    catalog = config.model_catalog()

    assert catalog.default_ref == "yuren/gpt-main"
    assert [item.ref for item in catalog.list()] == ["yuren/gpt-cheap", "yuren/gpt-main"]
    main = catalog.require("yuren/gpt-main")
    assert main.label == "Project label"
    assert main.provider.base_url == "https://project.example/v1"
    assert main.request.temperature == 0.2
    assert main.request.max_tokens == 8192
    assert main.request.extra_body == {"reasoning_effort": "high", "reasoning_summary": "auto"}


def test_model_catalog_rejects_model_without_declared_provider() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={},
        project_config={"models": {"missing/model": {}}},
    )

    with pytest.raises(ModelCatalogError, match="missing/model.*missing"):
        config.model_catalog()


def test_model_catalog_adapts_legacy_single_provider_config() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_API_KEY": "test-key"},
        project_config={
            "model": "yurenapi/gpt-legacy",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://example.test/v1",
                "api_key_env": "YUREN_API_KEY",
            },
        },
    )

    profile = config.model_catalog().require("yurenapi/gpt-legacy")

    assert profile.provider.type == "openai-compatible"
    assert profile.provider.base_url == "https://example.test/v1"
```

- [ ] **步骤 2：确认测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_config.py -q
```

预期：导入 `firstcoder.config.models` 失败，或 `AppConfig` 尚无 `model_catalog()`。

- [ ] **步骤 3：新增不可变配置模型与确定性深度合并**

创建 `firstcoder/config/models.py`。实现以下公开接口；所有返回的映射在构造时复制，避免模型切换改动已加载的配置。

```python
@dataclass(frozen=True, slots=True)
class ModelRequestOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


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

    def list(self) -> list[ModelProfile]: ...
    def get(self, ref: str) -> ModelProfile | None: ...
    def require(self, ref: str) -> ModelProfile: ...
```

实现 `_deep_merge_dicts(base, override)`：当两边同一键都是 `dict` 时递归合并，其余值由 `override` 整体替换。只合并 `providers` 和 `models`；在 `AppConfig` 上仍保留既有单值读取优先级，避免影响 MCP、权限或旧 provider 行为。

实现 `build_model_catalog(global_config, project_config, legacy_config)`，遵守以下校验：

```python
_RESERVED_REQUEST_EXTRA_BODY_FIELDS = {
    "model", "messages", "input", "tools", "tool_choice", "stream",
    "temperature", "max_tokens", "max_completion_tokens",
}
```

- 每个 `[providers.<id>]` 必须是表，并且 `type` 为非空字符串；项目配置可以省略已有全局 Provider 的 `type`。
- 每个 `[models."provider/model"]` 必须是表且指向存在 Provider；`model` 部分不能为空。
- `temperature` 必须是数字，`max_tokens` 必须是大于 0 的整数。
- `reasoning_effort = "high"` 被写入 `extra_body["reasoning_effort"]`；如果 `extra_body` 已有同键则抛出 `ModelCatalogError`，不静默决定优先级。
- `extra_body` 必须是表，且不得含 `_RESERVED_REQUEST_EXTRA_BODY_FIELDS` 中的键。
- 没有新格式 `models` 时，使用现有单 `model + [provider]` 配置生成一个兼容 `ModelProfile`；没有任何 provider 配置时保留既有 preset factory 的启动路径。

在 `AppConfig` 中新增：

```python
def model_catalog(self) -> ModelCatalog:
    return build_model_catalog(
        global_config=self.global_config,
        project_config=self.project_config,
        legacy_provider_name=self.provider_name,
        env=self.env,
    )
```

并在 `firstcoder/config/__init__.py` 导出 `ModelCatalog`、`ModelCatalogError`、`ModelProfile`。

- [ ] **步骤 4：运行配置测试并确认通过**

运行：

```sh
.venv/bin/python -m pytest tests/test_config.py -q
```

预期：退出码为 0，现有单 Provider 配置测试和新增 Catalog 测试同时通过。

- [ ] **步骤 5：提交解析层的独立改动**

```sh
git add firstcoder/config/models.py firstcoder/config/settings.py firstcoder/config/__init__.py tests/test_config.py
git commit -m "Add configurable model catalog"
```

### Task 2：让 Provider factory 按完整 ModelProfile 构造连接

**文件：**

- 修改：`firstcoder/providers/factory.py:36-169`
- 修改：`tests/test_config.py`

- [ ] **步骤 1：编写按 Profile 创建 Provider 的失败测试**

在 `tests/test_config.py` 添加：

```python
from firstcoder.providers.factory import create_provider_for_model


def test_create_provider_for_model_uses_profile_provider_and_model_options() -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_API_KEY": "test-key"},
        project_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key_env": "YUREN_API_KEY",
                    "parallel_tool_calls": True,
                }
            },
            "models": {"yuren/gpt-test": {}},
        },
    )

    provider = create_provider_for_model(config, config.model_catalog().require("yuren/gpt-test"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "yuren"
    assert provider.model == "gpt-test"
    assert provider.base_url == "https://example.test/v1"
    assert provider.capabilities.supports_parallel_tool_calls is True
```

- [ ] **步骤 2：确认 factory 测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_config.py::test_create_provider_for_model_uses_profile_provider_and_model_options -q
```

预期：`create_provider_for_model` 尚不存在。

- [ ] **步骤 3：实现 Profile 专用 factory，不替换旧 factory**

在 `firstcoder/providers/factory.py` 增加：

```python
def create_provider_for_model(config: AppConfig, profile: ModelProfile) -> ChatProvider:
    provider_config = profile.provider
    if provider_config.type in {"openai-compatible", "custom"}:
        return _create_catalog_openai_compatible(config, profile)
    if provider_config.type == "anthropic":
        return _create_catalog_anthropic(config, profile)
    if provider_config.type in PROVIDER_PRESETS:
        return _create_catalog_preset(config, profile)
    raise ProviderConfigError(f"不支持的 provider 类型：{provider_config.type}")
```

三个私有 helper 的共同规则：从 `profile.provider.api_key_env` 读取密钥，缺失时显示该环境变量名；将 `profile.model_id` 作为真实 SDK model；将 `profile.provider.id` 作为 OpenAI-compatible Provider 的显示名；用 `parallel_tool_calls` 和 `streaming` 构造能力覆盖。不要把 `profile.request.extra_body` 放到 Provider 构造函数：它只属于主 Agent 请求，不能污染摘要和任务边界分类。

保留 `create_provider()`、`create_provider_from_config()` 和所有旧 helper 的签名与行为，供 benchmark、旧 CLI `--provider` 和没有新格式模型目录的配置继续使用。

- [ ] **步骤 4：运行 factory 与旧配置回归测试**

运行：

```sh
.venv/bin/python -m pytest tests/test_config.py -q
```

预期：退出码为 0；新增 factory 测试证明 Provider、模型、endpoint 和并行工具能力均来自同一个 Profile。

- [ ] **步骤 5：提交 Provider 构造层**

```sh
git add firstcoder/providers/factory.py tests/test_config.py
git commit -m "Build providers from model profiles"
```

### Task 3：仅向主 Agent 请求注入 effort、token 和扩展参数

**文件：**

- 修改：`firstcoder/providers/types.py:136-145`
- 修改：`firstcoder/agent/loop.py:68-128,458-480,549-572`
- 修改：`firstcoder/app/runtime.py:77-105,253-269`
- 新增：`tests/test_model_request_options.py`
- 修改：`tests/test_providers.py:789-810`

- [ ] **步骤 1：为同步、流式和内部调用隔离写失败测试**

创建 `tests/test_model_request_options.py`，使用收集 `ChatRequest` 的 FakeProvider，并加入如下断言：

```python
def test_main_sync_request_inherits_selected_model_options(session: AgentSession) -> None:
    provider = RecordingProvider()
    loop = AgentLoop(
        session=session,
        provider=provider,
        request_options=MainRequestOptions(
            temperature=0.2,
            max_tokens=8192,
            extra_body={"reasoning_effort": "high"},
        ),
    )

    loop.run_user_turn("检查 README")

    request = provider.requests[-1]
    assert request.temperature == 0.2
    assert request.max_tokens == 8192
    assert request.extra_body == {"reasoning_effort": "high"}


def test_task_boundary_classifier_keeps_its_fixed_token_budget(session: AgentSession) -> None:
    classifier = TaskBoundaryClassifier(
        session=session,
        provider=RecordingProvider(),
        context_builder=ContextBuilder(),
        compact_if_needed=lambda: None,
        check_cancelled=lambda: None,
        check_turn_timeout=lambda: None,
        tag_task_boundary_messages=lambda: None,
    )

    request = classifier.build_request(attempt=0)

    assert request.max_tokens == 512
    assert request.temperature is None
    assert request.extra_body == {}
```

再加一个 `asyncio.run()` 的流式测试，断言 `AgentLoop._stream_once()` 发出的 `ChatRequest` 拥有同一套三项参数。

- [ ] **步骤 2：确认新增请求选项测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_model_request_options.py -q
```

预期：`MainRequestOptions` 和 `AgentLoop(request_options=...)` 尚不存在。

- [ ] **步骤 3：定义只服务主请求的不可变选项对象**

在 `firstcoder/providers/types.py` 的 `ChatRequest` 前增加：

```python
@dataclass(frozen=True, slots=True)
class MainRequestOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def as_chat_request_kwargs(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_body": dict(self.extra_body),
        }
```

`AgentLoop.__init__` 接收 `request_options: MainRequestOptions | None = None` 并保存 `self.request_options = request_options or MainRequestOptions()`。提取一个唯一 helper，确保同步和流式使用完全相同的注入逻辑：

```python
def _main_chat_request(self, messages, definitions, tool_choice) -> ChatRequest:
    return ChatRequest(
        messages=messages,
        tools=definitions,
        tool_choice=tool_choice,
        **self.request_options.as_chat_request_kwargs(),
    )
```

将 `_complete_once()` 和 `_stream_once()` 中的内联 `ChatRequest(...)` 全部替换为此 helper。不要修改 `TaskBoundaryClassifier.build_request()` 或 `ProviderLlmCompactSummarizer.summarize()`：它们继续使用各自固定 `max_tokens`，也不继承 `reasoning_effort`。

在 `AgentChatRunner` 新增 `request_options: MainRequestOptions = field(default_factory=MainRequestOptions)`，并让 `_create_loop()` 把它传给 `AgentLoop`。增加：

```python
def set_model(
    self,
    provider: ChatProvider,
    *,
    request_options: MainRequestOptions,
    use_streaming: bool,
) -> None:
    self.provider = provider
    self.request_options = request_options
    self.use_streaming = use_streaming
    self.last_stream_events = []
```

保留 `set_provider()`，但它调用 `set_model(..., request_options=MainRequestOptions())`，避免已有调用方意外继承旧模型的 effort。

- [ ] **步骤 4：补充底层 Provider 透传回归**

在 `tests/test_providers.py` 的 OpenAI-compatible 参数测试中增加：

```python
assert client.completions.last_params["extra_body"] == {
    "preset": True,
    "request": True,
    "reasoning_effort": "high",
}
```

对应请求使用：

```python
ChatRequest(
    messages=[ChatMessage(role="user", content="hi")],
    max_tokens=123,
    extra_body={"request": True, "reasoning_effort": "high"},
)
```

这锁定了既有 `OpenAICompatibleProvider` 的 `extra_body` 合并，不要求任何 Responses API 代码。

- [ ] **步骤 5：运行主请求与 Provider 透传测试**

运行：

```sh
.venv/bin/python -m pytest tests/test_model_request_options.py tests/test_providers.py -q
```

预期：退出码为 0；同步、流式主请求带入 Profile 参数，分类/压缩请求不带入，OpenAI-compatible client 收到 `reasoning_effort`。

- [ ] **步骤 6：提交请求参数链路**

```sh
git add firstcoder/providers/types.py firstcoder/agent/loop.py firstcoder/app/runtime.py tests/test_model_request_options.py tests/test_providers.py
git commit -m "Apply model request options to agent calls"
```

### Task 4：持久化项目级模型选择并实现启动解析

**文件：**

- 新增：`firstcoder/app/model_state.py`
- 修改：`firstcoder/app/factory.py:86-216,228-351`
- 新增：`tests/test_model_state.py`
- 修改：`tests/test_app_factory.py`

- [ ] **步骤 1：为选择状态与失效回退写失败测试**

创建 `tests/test_model_state.py`：

```python
def test_model_state_store_keeps_last_selected_and_deduplicated_recents(tmp_path: Path) -> None:
    store = ModelStateStore(tmp_path / "model_state.json")

    store.record_selection("yuren/gpt-main")
    store.record_selection("mimo/mimo-v2.5-pro")
    store.record_selection("yuren/gpt-main")

    state = store.load()
    assert state.last_selected == "yuren/gpt-main"
    assert state.recent == ("yuren/gpt-main", "mimo/mimo-v2.5-pro")


def test_model_state_store_treats_invalid_json_as_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text("{not-json", encoding="utf-8")

    assert ModelStateStore(path).load() == ModelSelectionState()
```

在 `tests/test_app_factory.py` 增加启动优先级测试：`--model` 覆盖 `default_model`；`default_model` 覆盖 state；state 中不存在于 Catalog 的模型会回退到 Catalog 第一个 Profile。

- [ ] **步骤 2：确认状态与启动测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_model_state.py tests/test_app_factory.py -q
```

预期：`ModelStateStore` 和 `create_firstcoder_app(..., model_spec=...)` 尚不存在。

- [ ] **步骤 3：实现原子 JSON 状态存储**

在 `firstcoder/app/model_state.py` 实现：

```python
@dataclass(frozen=True, slots=True)
class ModelSelectionState:
    last_selected: str | None = None
    recent: tuple[str, ...] = ()


class ModelStateStore:
    def __init__(self, path: Path, *, recent_limit: int = 10) -> None: ...
    def load(self) -> ModelSelectionState: ...
    def record_selection(self, ref: str) -> ModelSelectionState: ...
```

`record_selection()` 生成去重后的 `[ref, *old_recent]`，截断到十项；使用与目标同目录的 `tempfile.NamedTemporaryFile(delete=False)` 写 UTF-8 JSON、`flush()`、`os.fsync()`，再 `Path.replace()` 覆盖目标。读取不存在、无效 JSON、字段类型错误或空模型引用时返回空状态，不让一个 UI 偏好文件阻止 TUI 启动。

- [ ] **步骤 4：重构 app factory 的启动选择和切换器**

在 `create_firstcoder_app()` 增加可选参数：

```python
model_spec: str | None = None,
```

在已得到 `resolved_data_root` 和 `resolved_app_config` 后构造 `ModelStateStore(resolved_data_root / "model_state.json")`。当调用方没有注入 `provider` 且 Catalog 非空时，按下列 helper 选择并构造：

```python
def _initial_model_profile(
    catalog: ModelCatalog,
    *,
    model_spec: str | None,
    state: ModelSelectionState,
) -> ModelProfile:
    for ref in (model_spec, catalog.default_ref, state.last_selected):
        if ref and catalog.get(ref):
            return catalog.require(ref)
    profiles = catalog.list()
    if not profiles:
        raise ValueError("模型目录为空，且没有旧 Provider 配置可用")
    return profiles[0]
```

命中 Profile 时调用 `create_provider_for_model()`，并把 Profile 的 `ModelRequestOptions` 转换为 `MainRequestOptions` 传入 `AgentChatRunner`。没有新格式 Catalog 时仍走当前 `create_provider(project_root=...)` 旧路径。

把 `RuntimeModelSwitcher` 改为持有 `ModelCatalog` 和 `ModelStateStore`。它对显式 Catalog ref 的切换必须：

1. `catalog.require(spec)`；
2. `create_provider_for_model()`；
3. `chat_runner.set_model(provider, request_options=..., use_streaming=...)`；
4. `compact_summarizer.provider = provider`；
5. `state_store.record_selection(profile.ref)`；
6. 返回 `ModelState(provider=provider.name, model=provider.model)`。

如果输入没有 `/`，使用当前 Profile 的 Provider 配置创建一个不进 Catalog、不写状态的临时 ModelProfile；如果输入带 `/` 却不在 Catalog，报错 `未配置模型：<ref>。请在 [models] 中添加它。`。

- [ ] **步骤 5：运行状态与 factory 测试**

运行：

```sh
.venv/bin/python -m pytest tests/test_model_state.py tests/test_app_factory.py -q
```

预期：退出码为 0；状态文件 round-trip、损坏回退、四级启动选择和切换同步均通过。

- [ ] **步骤 6：提交项目级选择状态**

```sh
git add firstcoder/app/model_state.py firstcoder/app/factory.py tests/test_model_state.py tests/test_app_factory.py
git commit -m "Persist selected catalog model"
```

### Task 5：让 TUI 选择器真正展示 Catalog，并统一 `/models` 命令

**文件：**

- 修改：`firstcoder/app/model_commands.py:11-89`
- 修改：`tests/test_app_model_commands.py`
- 修改：`tests/test_app_tui.py`（仅在现有 model picker 断言处）

- [ ] **步骤 1：为 plural 命令、列表和未知模型写失败测试**

在 `tests/test_app_model_commands.py` 添加：

```python
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


def test_model_command_preserves_catalog_switch_error() -> None:
    result = ModelCommandHandler(FakeSwitcher(error=ValueError("未配置模型：missing/model"))).handle(
        "/model missing/model"
    )

    assert result.output == "Model switch failed: 未配置模型：missing/model"
```

- [ ] **步骤 2：确认命令测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_app_model_commands.py -q
```

预期：`/models` 被忽略，新增别名测试失败。

- [ ] **步骤 3：实现无歧义命令语义**

修改 `ModelCommandHandler.handle()` 的分支条件：

```python
if command in {"/model", "/models"}:
    return self._picker_result()
if not command.startswith("/model "):
    return CommandResult(handled=False)
```

保留 `/model <ref>` 作为直接切换命令，输出与当前 `model_changed` action 形状完全一致，使 `firstcoder/app/tui.py` 不需要改变 Picker action 协议。把 picker 尾部帮助文本改为：

```text
Use up/down and enter to switch, or type /model <provider>/<model>.
```

不要让 `/models <ref>` 成为第二种切换命令，避免同一功能出现两个参数语法。

- [ ] **步骤 4：运行命令和 TUI 回归测试**

运行：

```sh
.venv/bin/python -m pytest tests/test_app_model_commands.py tests/test_app_tui.py -q
```

预期：退出码为 0；`/model`、`/models` 都打开现有 picker，选择后的 topbar 更新仍由原 `model_changed` action 驱动。

- [ ] **步骤 5：提交 TUI 命令层**

```sh
git add firstcoder/app/model_commands.py tests/test_app_model_commands.py tests/test_app_tui.py
git commit -m "Expose catalog models in TUI picker"
```

### Task 6：提供 CLI 覆盖、可诊断配置输出和双语文档

**文件：**

- 修改：`firstcoder/cli.py:22-80,102-152,192-241`
- 修改：`tests/test_cli.py`
- 修改：`README.md:106-132`
- 修改：`README.zh-CN.md:106-132`

- [ ] **步骤 1：编写 CLI 覆盖和无密钥诊断输出的失败测试**

在 `tests/test_cli.py` 添加：

```python
def test_parser_accepts_model_catalog_override() -> None:
    args = build_parser().parse_args(["--model", "yuren/gpt-main", "--message", "hi"])

    assert args.model == "yuren/gpt-main"


def test_config_show_lists_catalog_refs_without_api_keys(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "firstcoder.toml").write_text(
        "[providers.yuren]\ntype = 'openai-compatible'\napi_key_env = 'YUREN_API_KEY'\n"
        "[models.'yuren/gpt-main']\n",
        encoding="utf-8",
    )

    assert main(["--project", str(tmp_path), "config", "show"]) == 0

    output = capsys.readouterr().out
    assert "models:" in output
    assert "  - yuren/gpt-main" in output
    assert "YUREN_API_KEY=" not in output
```

- [ ] **步骤 2：确认 CLI 测试先失败**

运行：

```sh
.venv/bin/python -m pytest tests/test_cli.py -q
```

预期：`--model` 为未知参数，或 `config show` 尚未打印 `models:`。

- [ ] **步骤 3：实现 CLI 模型覆盖和安全诊断**

给 parser 添加：

```python
parser.add_argument(
    "--model",
    default=None,
    help="Configured model reference override: provider/model.",
)
```

在 `CliConfig` 新增 `model_spec: str | None`，所有三处构造 `CliConfig` 时传入 `args.model`，并让 `create_cli_app()` 调用：

```python
create_firstcoder_app(
    project_root=config.project_root,
    data_root=config.data_root,
    provider=provider,
    session_id=config.session_id,
    model_spec=config.model_spec,
)
```

`config show` 保留当前 provider/model/base_url/parallel 输出，并在 Catalog 不为空时追加：

```text
default_model: yuren/gpt-main
models:
  - mimo/mimo-v2.5-pro
  - yuren/gpt-main
```

不得打印 `api_key`、环境变量值、`extra_body` 的潜在敏感字段或 `model_state.json` 内容。

- [ ] **步骤 4：补充双语 README 中的可复制配置和命令示例**

在两份 README 的 Configuration/配置小节加入 Task 目标配置格式的精简版，并列出：

```sh
firstcoder --model yuren/gpt-5.6-terra --tui
# TUI 内：/models 或 /model yuren/gpt-5.6-terra
firstcoder --project . config show
```

英文 README 说明 `reasoning_effort` 通过 Chat Completions-compatible request extra body 发送，provider support varies；中文 README 作等义说明。两份文档都明确：当前不是 Responses API，模型切换不会改写 TOML 或输出 API key。

- [ ] **步骤 5：运行 CLI、文档和核心回归**

运行：

```sh
.venv/bin/python -m pytest tests/test_cli.py tests/test_config.py tests/test_app_factory.py tests/test_app_model_commands.py tests/test_model_request_options.py -q
.venv/bin/python -m pytest tests/test_readme_provider_docs.py -q
git diff --check
```

预期：三条命令均以退出码 0 完成，且 `git diff --check` 无输出。

- [ ] **步骤 6：提交 CLI 与文档**

```sh
git add firstcoder/cli.py tests/test_cli.py README.md README.zh-CN.md
git commit -m "Document model catalog configuration"
```

### Task 7：做完整验证并记录明确的未覆盖边界

**文件：**

- 修改：本计划文件的“验证记录”小节（仅在实施完成后勾选和填写实际命令结果）

- [ ] **步骤 1：按顺序运行完整 pytest**

运行：

```sh
.venv/bin/python -m pytest
```

预期：退出码为 0。若失败，先用同一个失败测试在改动前的 `HEAD` 复现，区分基线失败和本功能回归；不要将未复现的失败描述为本次功能完成。

- [ ] **步骤 2：对实际配置做不泄漏密钥的启动诊断**

运行：

```sh
.venv/bin/python -m firstcoder --project . config show
```

预期：只显示模型引用、Provider 名、base URL、布尔能力和已加载配置文件路径；不显示 API key、环境变量值或请求正文。

- [ ] **步骤 3：人工验证 TUI 的两个切换入口**

运行：

```sh
.venv/bin/python -m firstcoder --project . --tui
```

在 TUI 内依次输入 `/models`，从 picker 选择一个已配置的模型；然后输入 `/model <另一个已配置 provider/model>`。两次均应立即更新顶栏；下一轮主 Agent 请求使用被选模型的 `temperature`、`max_tokens`、`reasoning_effort`。不要在真实密钥不可用时把连接失败归因于切换功能，应先确认 `config show` 的模型引用与 `api_key_env`。

- [ ] **步骤 4：最终提交前检查工作区范围**

运行：

```sh
git status --short
git diff --check
```

预期：本功能只包含上述文件；保留并避开本工作区既有的 `.gitignore`、`.release-dist*` 和无关 `docs/superpowers` 草稿改动。

## 验证记录（实施时填写）

- [ ] `tests/test_config.py`：
- [ ] `tests/test_model_request_options.py`：
- [ ] `tests/test_model_state.py`：
- [ ] `tests/test_app_factory.py`、`tests/test_app_model_commands.py`、`tests/test_app_tui.py`：
- [ ] `tests/test_cli.py`、`tests/test_readme_provider_docs.py`：
- [ ] 全量 `.venv/bin/python -m pytest`：
- [ ] `git diff --check`：

## 实施后必须仍然成立的事实

- 现有 `OpenAICompatibleProvider` 继续调用 `client.chat.completions.create(...)`；没有任何调用迁移到 Responses API。
- 新 Catalog 只配置和选择 FirstCoder 已能支持的 Provider；不把未配置 preset 默认模型伪装成可用模型。
- 每次模型切换同时更新主 `AgentChatRunner`、流式开关和 L4 compact summarizer 的 Provider。
- `reasoning_effort`、`temperature`、`max_tokens` 只用于主 Agent 的模型请求；任务边界分类仍为 512 tokens，L4 摘要仍为 1200 tokens。
- API key 继续来自环境变量或 `.env`，不会被写入配置模板、状态文件、日志诊断或 README 示例。
