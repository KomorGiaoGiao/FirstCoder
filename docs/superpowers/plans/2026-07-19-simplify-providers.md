# Provider Protocol Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce duplicated provider streaming and usage primitives without merging vendor-specific protocol adapters.

**Architecture:** Move only SDK-object/dict field access, token-usage construction/merge, accumulator storage, and safe JSON completion into `providers/streaming.py`. OpenAI and Anthropic retain their event loops, request formats, finish-reason mappings, and vendor diagnostics.

**Tech Stack:** Python 3.11+, pytest, OpenAI/Anthropic fake clients

---

### Task 1: Characterize shared usage semantics

**Files:**
- Modify: `tests/test_providers.py`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Add cross-provider usage tests**

Add a parametrized test proving both providers produce identical `TokenUsage` values from their native field names and that partial streamed usage merges by preferring newer non-`None` values.

```python
@pytest.mark.parametrize(
    ("provider", "usage"),
    [
        ("openai", {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
        ("anthropic", {"input_tokens": 3, "output_tokens": 4}),
    ],
)
def test_provider_usage_normalizes_to_shared_token_usage(provider, usage):
    response = _complete_with_usage(provider, usage)
    assert response.usage == TokenUsage(input_tokens=3, output_tokens=4, total_tokens=7)
```

- [ ] **Step 2: Verify the test can fail**

Temporarily assert `total_tokens == 8`; run the new test and expect failures. Restore `7` and expect passes.

### Task 2: Move token usage primitives to streaming.py

**Files:**
- Modify: `firstcoder/providers/streaming.py`
- Modify: `firstcoder/providers/openai_compatible.py`
- Modify: `firstcoder/providers/anthropic_provider.py`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Add vendor-neutral helpers**

```python
def token_usage(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None = None,
) -> TokenUsage | None:
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is output_tokens is total_tokens is None:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)

def merge_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage | None:
    if left is None or right is None:
        return right or left
    return token_usage(
        right.input_tokens if right.input_tokens is not None else left.input_tokens,
        right.output_tokens if right.output_tokens is not None else left.output_tokens,
        right.total_tokens if right.total_tokens is not None else left.total_tokens,
    )
```

- [ ] **Step 2: Replace local construction and delete duplicate merge code**

Keep `_parse_usage()` in each adapter because field names differ, but make it return `token_usage(...)`. Import `merge_usage` only in Anthropic where stream events need it.

- [ ] **Step 3: Run usage tests**

```sh
.venv/bin/python -m pytest tests/test_providers.py -q -k 'usage'
```

Expected: all selected tests pass.

### Task 3: Share streaming tool-call completion

**Files:**
- Modify: `firstcoder/providers/streaming.py`
- Modify: `firstcoder/providers/openai_compatible.py`
- Modify: `firstcoder/providers/anthropic_provider.py`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Add a shared accumulator DTO**

```python
@dataclass(slots=True)
class StreamToolCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments_text: str = ""
    saw_arguments: bool = False
```

- [ ] **Step 2: Add safe completion with an explicit identity policy**

```python
def complete_stream_tool_calls(
    accumulators: Mapping[int, StreamToolCallAccumulator],
    diagnostics: ProviderDiagnostics,
    *,
    require_identity: bool,
) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for index in sorted(accumulators):
        item = accumulators[index]
        missing_identity = require_identity and (not item.id or not item.name)
        if missing_identity or not item.saw_arguments:
            missing = "id、name 或 arguments" if require_identity else "arguments"
            diagnostics.warnings.append(
                f"streaming tool_call 缺少 {missing}，已丢弃整组不可执行调用："
                f"index={index}, id={item.id}, name={item.name}"
            )
            return []
        arguments = loads_json_object(item.arguments_text)
        if not isinstance(arguments, dict):
            diagnostics.warnings.append(
                f"streaming tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用："
                f"index={index}, id={item.id}, name={item.name}"
            )
            return []
        parsed.append(ToolCall(id=item.id, name=item.name, arguments=arguments))
    return parsed
```

`require_identity=True` preserves OpenAI's requirement for id/name/arguments. `False` preserves Anthropic's current requirement that arguments exist. Add the required `Mapping`, `ProviderDiagnostics`, `ToolCall`, and `loads_json_object` imports to `streaming.py`.

- [ ] **Step 3: Delete both private accumulator classes and completion functions**

Update type names and calls only. Do not move OpenAI delta parsing or Anthropic event traversal.

- [ ] **Step 4: Run streaming tool tests**

```sh
.venv/bin/python -m pytest tests/test_providers.py -q -k 'streaming_tool or streams_reasoning or truncated_streaming'
```

Expected: all selected tests pass.

### Task 4: Verify and measure the provider batch

**Files:**
- Modify: `docs/superpowers/plans/2026-07-19-simplify-providers.md`

- [ ] **Step 1: Run provider and full tests**

```sh
.venv/bin/python -m pytest tests/test_providers.py tests/test_provider_errors.py tests/test_model_request_options.py -q
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q firstcoder
git diff --check
```

Expected: all pass.

- [ ] **Step 2: Measure and commit**

```sh
find firstcoder -name '*.py' -type f -print0 | xargs -0 wc -l | tail -n 1
git diff --numstat -- firstcoder/providers
git add firstcoder/providers tests/test_providers.py docs/superpowers/plans/2026-07-19-simplify-providers.md
git commit -m "Share provider streaming primitives"
```

Do not keep a shared helper if imports plus policy callbacks yield no net production-code reduction.

## Execution record

- Added cross-provider usage characterization: red run `2 failed`, corrected run `2 passed`.
- Shared usage selection: `3 passed, 35 deselected`.
- Shared streaming tool-call selection: `9 passed, 29 deselected`.
- Provider-focused suite: `46 passed`.
- Full suite: `1187 passed, 30 warnings`.
- `compileall` and `git diff --check`: exit 0.
- Shared `token_usage`, `merge_usage`, `StreamToolCallAccumulator`, and `complete_stream_tool_calls` in `providers/streaming.py`; vendor event traversal and finish-reason mapping remain separate.
- Production total after this batch: 25,551 lines; this batch net reduction: 34 lines; cumulative reduction from 25,616 baseline: 65 lines.
