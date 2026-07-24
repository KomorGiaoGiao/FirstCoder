# Skill 全量发现与 MCP 工具按需暴露实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保证所有 Skill 至少以名称进入模型目录，并把 MCP 从全量 schema 常驻改为本地搜索后仅在当前用户回合暴露最多 8 个命中工具。

**Architecture:** Skill 继续使用现有 `resolve_skill_catalog -> render_skill_catalog -> load_skill` 链路，只把目录预算改成“名称优先、描述公平分配”。MCP 继续把 executor 注册进现有 `ToolRegistry`，新增无网络的 `mcp_tool_search`；`AgentLoop` 单独维护回合内激活名称集合，只把命中 definition 发给 provider，并在权限预检前拒绝模型猜测的未激活 MCP 调用。

**Tech Stack:** Python 3.11+、dataclasses、pytest、现有 `Tool`/`ToolRegistry`/`AgentLoop`/`McpManager`/OpenAI-compatible/Anthropic provider 适配层。

---

## 实施边界

本计划以已确认设计为准：

- 设计说明：`docs/superpowers/specs/2026-07-24-skill-mcp-progressive-disclosure-design.zh-CN.md`
- 不修改 MCP transport、连接重试、权限决策、session event schema、checkpoint 或 provider wire protocol。
- 不引入 embedding、向量数据库、外部检索、provider 专属 `defer_loading`。
- `enabled` 和 `allowed_tools` 继续先于搜索生效。
- 搜索命中的 schema 只在当前新用户消息对应的回合有效；权限确认复用同一 `AgentLoop`，因此保留；普通 `ask_user` 回答是下一条用户消息，因此清空。
- 自定义 `tools` 模式继续不自动追加 MCP 或 `mcp_tool_search`。

## 文件职责与预计改动

- Modify: `firstcoder/skills/catalog.py` — 名称优先的 Skill 目录预算，不承担搜索。
- Create: `firstcoder/mcp/search.py` — MCP 候选记录、确定性评分、`mcp_tool_search` 工具构造。
- Modify: `firstcoder/app/factory.py` — 只在默认工具模式且存在有效 MCP 候选时装配搜索工具。
- Modify: `firstcoder/agent/loop.py` — 回合内 MCP 激活集合和 provider definition 过滤。
- Modify: `firstcoder/agent/tool_execution.py` — 在权限预检前调用窄的 tool-call validator，并在落盘后通知 result observer。
- Modify: `docs/SKILL_SYSTEM_DESIGN.zh-CN.md`、`docs/SKILL_SYSTEM_DESIGN.md` — 记录名称优先预算。
- Modify: `docs/MCP.zh-CN.md`、`docs/MCP.md` — 记录搜索、激活周期和权限边界。
- Test: `tests/test_skill_discovery.py` — Skill 目录预算和极端溢出。
- Create: `tests/test_mcp_search.py` — 纯搜索、上限、稳定排序和安全结果。
- Modify: `tests/test_app_factory.py` — 默认/自定义/失败装配。
- Modify: `tests/test_agent_context_loop.py` — schema 过滤、生命周期、调用守卫、同步/流式路径。
- Modify: `tests/test_app_runtime.py` — 权限暂停恢复复用激活状态。
- Modify: `tests/test_mcp_integration.py` — 激活后的调用仍走精确 MCP 权限。

---

### Task 1: 让 Skill 目录优先保留所有名称

**Files:**

- Modify: `firstcoder/skills/catalog.py`
- Test: `tests/test_skill_discovery.py`

- [ ] **Step 1: 写“所有名称可见且总长不超预算”的失败测试**

在 `tests/test_skill_discovery.py` 中把当前只检查完整行的长目录测试收紧，并补一个少量 Skill 的描述上限测试：

```python
from firstcoder.skills.catalog import (
    SKILL_CATALOG_MAX_CHARS,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_LOAD_INSTRUCTION,
    render_skill_catalog,
)


def test_render_skill_catalog_keeps_every_skill_name_within_budget() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}",
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL_AGENT_SKILL,
            root="/global",
            description="A deliberately long description. " * 40,
        )
        for index in range(100)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))

    assert len(rendered) <= SKILL_CATALOG_MAX_CHARS
    assert rendered.splitlines()[-1] == SKILL_LOAD_INSTRUCTION
    for skill in skills:
        assert f"- {skill.name}:" in rendered


def test_render_skill_catalog_keeps_full_description_budget_for_small_catalog() -> None:
    description = "x" * (SKILL_DESCRIPTION_MAX_CHARS + 20)
    skill = SkillDefinition(
        name="review",
        path="review/SKILL.md",
        source=SkillSource.GLOBAL_AGENT_SKILL,
        root="/global",
        description=description,
    )

    line = render_skill_catalog(SkillCatalog(skills=[skill])).splitlines()[0]

    assert line == f"- review: {'x' * (SKILL_DESCRIPTION_MAX_CHARS - 3)}..."
```

- [ ] **Step 2: 运行测试，确认旧算法会丢失后半名称**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_skill_discovery.py::test_render_skill_catalog_keeps_every_skill_name_within_budget \
  tests/test_skill_discovery.py::test_render_skill_catalog_keeps_full_description_budget_for_small_catalog -q
```

Expected: 第一条 FAIL，错误显示后半部分 `skill-*` 不在渲染结果中；第二条保持 PASS。

- [ ] **Step 3: 写极端“名称骨架本身溢出”的失败测试**

在同一文件增加：

```python
def test_render_skill_catalog_extreme_name_overflow_keeps_whole_lines_and_warning() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}-" + "n" * 400,
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL_AGENT_SKILL,
            root="/global",
            description="description must not steal name budget",
        )
        for index in range(30)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))
    lines = rendered.splitlines()

    assert len(rendered) <= SKILL_CATALOG_MAX_CHARS
    assert lines[-1] == SKILL_LOAD_INSTRUCTION
    assert "Skill catalog truncated:" in lines[-2]
    assert all(line.endswith(":") for line in lines[:-2])
    assert all(line in {f"- {skill.name}:" for skill in skills} for line in lines[:-2])
```

- [ ] **Step 4: 运行极端测试，确认当前实现没有截断提示**

Run:

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py::test_render_skill_catalog_extreme_name_overflow_keeps_whole_lines_and_warning -q
```

Expected: FAIL，因为当前实现既允许 description 抢预算，也没有明确的目录截断提示。

- [ ] **Step 5: 实现两阶段预算和完整行兜底**

在 `firstcoder/skills/catalog.py` 保持 `resolve_skill_catalog()` 不变，替换目录渲染 helper。实现时使用统一的字符计数函数，不能先截断后再猜长度：

```python
SKILL_CATALOG_TRUNCATED = "Skill catalog truncated: not every skill name fits the catalog budget."


def render_skill_catalog(catalog: SkillCatalog) -> str:
    skills = resolve_skill_catalog(catalog).skills
    if not skills:
        return SKILL_LOAD_INSTRUCTION

    footer = SKILL_LOAD_INSTRUCTION
    name_lines = [f"- {skill.name}:" for skill in skills]
    # Every normal row has one separator space before its description. Reserve
    # those spaces even when the description later becomes empty.
    fixed_cost = _joined_length([*name_lines, footer]) + len(skills)
    if fixed_cost > SKILL_CATALOG_MAX_CHARS:
        return _render_name_only_prefix(name_lines)

    description_budget = SKILL_CATALOG_MAX_CHARS - fixed_cost
    per_skill_limit = min(
        SKILL_DESCRIPTION_MAX_CHARS,
        description_budget // len(skills),
    )
    lines = [
        _catalog_line(skill, description_limit=per_skill_limit)
        for skill in skills
    ]
    return "\n".join([*lines, footer])


def _joined_length(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def _catalog_line(skill: SkillDefinition, *, description_limit: int) -> str:
    prefix = f"- {skill.name}:"
    description = " ".join(skill.description.split()) or "No description provided."
    rendered = _truncate_description(description, description_limit)
    return f"{prefix} {rendered}" if rendered else prefix


def _truncate_description(description: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(description) <= limit:
        return description
    if limit <= 3:
        return "." * limit
    return description[: limit - 3].rstrip() + "..."


def _render_name_only_prefix(name_lines: list[str]) -> str:
    footer = [SKILL_CATALOG_TRUNCATED, SKILL_LOAD_INSTRUCTION]
    lines: list[str] = []
    for line in name_lines:
        if _joined_length([*lines, line, *footer]) > SKILL_CATALOG_MAX_CHARS:
            break
        lines.append(line)
    return "\n".join([*lines, *footer])
```

实现后检查：普通路径中 `description_budget` 已经扣除了所有换行；极端路径永不输出半截名称；footer 永远完整。

- [ ] **Step 6: 跑 Skill 聚焦测试**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_skill_discovery.py \
  tests/test_skill_loader.py \
  tests/test_agent_skill_flow.py \
  tests/test_context_system_prompt.py -q
```

Expected: PASS；原有同名优先级、空白归一化、`load_skill` 和 system prompt 测试不回归。

- [ ] **Step 7: 提交 Skill 预算改动**

```sh
git add firstcoder/skills/catalog.py tests/test_skill_discovery.py
git commit -m "Keep all skill names visible"
```

---

### Task 2: 增加纯本地、确定性的 MCP 工具搜索

**Files:**

- Create: `firstcoder/mcp/search.py`
- Create: `tests/test_mcp_search.py`

- [ ] **Step 1: 写搜索排序、稳定同分和 8 个上限的失败测试**

创建 `tests/test_mcp_search.py`。测试只构造 `McpSearchEntry`，不启动 transport：

```python
from firstcoder.mcp.search import (
    MCP_TOOL_SEARCH_LIMIT,
    McpSearchEntry,
    create_mcp_tool_search,
    search_mcp_tools,
)
from firstcoder.providers.types import ToolDefinition


def _entry(server: str, tool: str, description: str) -> McpSearchEntry:
    return McpSearchEntry(
        server=server,
        tool=tool,
        definition=ToolDefinition(
            name=f"mcp__{server}__{tool}",
            description=description,
            parameters={"type": "object", "properties": {}},
        ),
    )


def test_search_mcp_tools_prefers_exact_name_then_name_server_and_description() -> None:
    entries = (
        _entry("github", "get_pull_request", "Read one pull request."),
        _entry("github", "search_pull_requests", "Search pull requests."),
        _entry("tracker", "lookup", "Get a GitHub pull request."),
    )

    matches = search_mcp_tools(entries, "get pull request")

    assert [item.tool for item in matches] == [
        "get_pull_request",
        "search_pull_requests",
        "lookup",
    ]


def test_search_mcp_tools_is_stable_and_limited() -> None:
    entries = tuple(
        _entry("demo", f"lookup_{index:02d}", "Lookup records.")
        for index in reversed(range(20))
    )

    matches = search_mcp_tools(entries, "lookup")

    assert len(matches) == MCP_TOOL_SEARCH_LIMIT == 8
    assert [item.definition.name for item in matches] == sorted(
        item.definition.name for item in matches
    )
```

- [ ] **Step 2: 写搜索工具结果结构和安全边界测试**

继续增加：

```python
def test_mcp_tool_search_returns_activated_names_without_executing_mcp() -> None:
    entries = (_entry("github", "get_issue", "Read one issue."),)
    tool = create_mcp_tool_search(entries)

    result = tool.executor(query="read github issue")

    assert result.ok is True
    assert result.data["mcp_tool_search"]["activated_tools"] == [
        "mcp__github__get_issue"
    ]
    assert "mcp__github__get_issue" in result.content
    assert tool.permission is None


def test_mcp_tool_search_rejects_blank_query_and_handles_no_match() -> None:
    tool = create_mcp_tool_search(
        (_entry("github", "get_issue", "Read one issue."),)
    )

    blank = tool.executor(query="   ")
    missing = tool.executor(query="calendar event")

    assert blank.ok is False
    assert blank.data["mcp_tool_search"]["activated_tools"] == []
    assert missing.ok is True
    assert missing.data["mcp_tool_search"]["activated_tools"] == []
```

同时断言 definition 只有必填字符串 `query`、`additionalProperties=False`，且没有模型可控 `limit`。

- [ ] **Step 3: 运行新测试，确认模块不存在**

Run:

```sh
.venv/bin/python -m pytest tests/test_mcp_search.py -q
```

Expected: collection ERROR，提示 `firstcoder.mcp.search` 不存在。

- [ ] **Step 4: 实现搜索记录、评分和工具构造**

创建 `firstcoder/mcp/search.py`，保持它不依赖 `McpManager`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result

MCP_TOOL_SEARCH_NAME = "mcp_tool_search"
MCP_TOOL_SEARCH_LIMIT = 8
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class McpSearchEntry:
    server: str
    tool: str
    definition: ToolDefinition


def search_mcp_tools(
    entries: tuple[McpSearchEntry, ...],
    query: str,
) -> tuple[McpSearchEntry, ...]:
    normalized_query = _normalize(query)
    query_tokens = set(_tokens(query))
    ranked: list[tuple[int, str, McpSearchEntry]] = []
    for entry in entries:
        score = _score(entry, normalized_query, query_tokens)
        if score > 0:
            ranked.append((-score, entry.definition.name, entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:MCP_TOOL_SEARCH_LIMIT])


def create_mcp_tool_search(entries: tuple[McpSearchEntry, ...]) -> Tool:
    def execute(*, query: str) -> ToolResult:
        if not query.strip():
            return make_error_result(
                MCP_TOOL_SEARCH_NAME,
                "MCP tool search query must not be blank.",
                mcp_tool_search={"activated_tools": []},
            )
        matches = search_mcp_tools(entries, query)
        activated = [entry.definition.name for entry in matches]
        content = _render_matches(matches)
        return ToolResult(
            name=MCP_TOOL_SEARCH_NAME,
            ok=True,
            content=content,
            data={"mcp_tool_search": {"activated_tools": activated}},
        )

    return Tool(
        definition=ToolDefinition(
            name=MCP_TOOL_SEARCH_NAME,
            description=(
                "Search connected MCP tools by capability. Matching tool schemas "
                "become available for the remainder of the current user turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Describe the external capability or MCP operation needed.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        executor=execute,
    )
```

`_score()` 使用明确权重维持已确认的优先级：完整 tool/模型可见名称匹配 `10_000`，tool name token 每个 `100`，server token 每个 `20`，description token 每个 `1`。`_tokens()` 对 `_`、`-` 和 Unicode 文本拆词，`_render_matches()` 每个匹配只输出模型可见名称、`server/tool` 和折叠后的短 description。不要把 input schema 或配置值放入 tool result。

- [ ] **Step 5: 跑搜索测试和 MCP 既有测试**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_mcp_search.py \
  tests/test_mcp_adapter.py \
  tests/test_mcp_manager.py \
  tests/test_mcp_config.py -q
```

Expected: PASS；搜索模块没有 transport、权限管理器或网络依赖。

- [ ] **Step 6: 提交纯搜索组件**

```sh
git add firstcoder/mcp/search.py tests/test_mcp_search.py
git commit -m "Add deterministic MCP tool search"
```

---

### Task 3: 在默认装配中注册 MCP 搜索而不改变 executor 注册

**Files:**

- Modify: `firstcoder/app/factory.py`
- Test: `tests/test_app_factory.py`

- [ ] **Step 1: 写默认装配与搜索目录一致性的失败测试**

调整 `tests/test_app_factory.py::test_factory_connects_mcp_once_and_merges_discovered_tools`，保留 MCP executor 已注册断言，并增加：

```python
names = app.current_session.session.tool_registry.names()
assert "mcp__demo__ping" in names
assert "mcp_tool_search" in names

search = app.current_session.session.tool_registry.get("mcp_tool_search")
result = search.executor(query="ping demo")
assert result.data["mcp_tool_search"]["activated_tools"] == [
    "mcp__demo__ping"
]
```

增加无 MCP、连接失败与自定义工具断言：

```python
assert "mcp_tool_search" not in app.current_session.session.tool_registry.names()
```

并增加两个同名/非法 MCP 描述的假目录，断言搜索结果只包含真正成功通过 `adapt_mcp_tool()` 的工具，不能搜索到 factory 已跳过的候选。

- [ ] **Step 2: 运行 factory 测试，确认搜索工具尚未装配**

Run:

```sh
.venv/bin/python -m pytest tests/test_app_factory.py -q
```

Expected: FAIL，默认模式 registry 中缺少 `mcp_tool_search`。

- [ ] **Step 3: 让 `McpToolProvider` 从成功适配项构造搜索目录**

在 `firstcoder/app/factory.py` 导入 `McpSearchEntry` 和 `create_mcp_tool_search`。保持 `McpToolProvider.__call__()` 返回一张普通 `list[Tool]`，只在适配循环后增加：

```python
entries: list[McpSearchEntry] = []
for server, discovered_tool in catalog:
    try:
        tool = adapt_mcp_tool(
            self._manager,
            server,
            discovered_tool,
            existing_names=names,
        )
    except ValueError:
        continue
    tools.append(tool)
    names.add(tool.name)
    entries.append(
        McpSearchEntry(
            server=server,
            tool=discovered_tool.name,
            definition=tool.definition,
        )
    )
if entries:
    tools.append(create_mcp_tool_search(tuple(entries)))
```

继续由 `include_mcp=tools is None` 控制默认/自定义模式；不要修改 `McpManagerLike` 或 `McpManager.tools()` 协议。

- [ ] **Step 4: 跑装配、new/resume/fork 聚焦测试**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_app_factory.py \
  tests/test_app_session_commands.py \
  tests/test_app_session_commands.py \
  tests/test_session_resume_service.py \
  tests/test_session_fork.py -q
```

Expected: PASS；新建、resume、fork 都通过同一个 `tools_provider` 得到相同搜索能力，自定义工具模式仍不追加 MCP。

- [ ] **Step 5: 提交装配改动**

```sh
git add firstcoder/app/factory.py tests/test_app_factory.py
git commit -m "Wire MCP search into default tools"
```

---

### Task 4: 在 AgentLoop 中实施回合级 schema 激活和调用守卫

**Files:**

- Modify: `firstcoder/agent/loop.py`
- Modify: `firstcoder/agent/tool_execution.py`
- Test: `tests/test_agent_context_loop.py`
- Test: `tests/test_app_runtime.py`
- Test: `tests/test_mcp_integration.py`

- [ ] **Step 1: 写“首次隐藏、搜索后只出现命中项”的同步失败测试**

在 `tests/test_agent_context_loop.py` 增加一个 fake MCP caller 和两个由 `adapt_mcp_tool()` 创建的工具；搜索工具使用 Task 2 构造函数。provider 依次返回搜索调用、目标 MCP 调用和最终回答：

```python
def test_agent_loop_exposes_only_searched_mcp_schemas_for_current_turn(tmp_path) -> None:
    # tools: mcp__github__get_issue, mcp__github__create_issue, mcp_tool_search
    # provider responses: search -> get_issue -> final text
    result = AgentLoop(session=session, provider=provider, tools=tools).run_user_turn(
        "Read issue 12"
    )

    first_names = {tool.name for tool in provider.requests[0].tools}
    second_names = {tool.name for tool in provider.requests[1].tools}

    assert "mcp_tool_search" in first_names
    assert not any(name.startswith("mcp__") for name in first_names)
    assert "mcp__github__get_issue" in second_names
    assert "mcp__github__create_issue" not in second_names
    assert result.content == "done"
```

测试使用 bypass 权限模式或一个无需真实副作用的 fake permission manager，避免测试目标被确认 UI 混淆。

- [ ] **Step 2: 写新用户消息清空与同回合累积测试**

复用同一个 `AgentLoop`：

```python
loop.run_user_turn("Read issue 12")
loop.run_user_turn("Explain this local function")

next_turn_names = {tool.name for tool in provider.requests[-1].tools}
assert "mcp_tool_search" in next_turn_names
assert not any(name.startswith("mcp__") for name in next_turn_names)
```

另一个用例在同一回合连续搜索两次，断言激活集合取并集，但每次搜索仍最多增加 8 个。

- [ ] **Step 3: 写未激活 MCP 名称猜测的失败测试**

让 provider 第一响应直接调用已注册但未搜索的 `mcp__github__get_issue`：

```python
assert caller.calls == []
tool_part = next(
    part
    for message in session.rebuild_view().messages
    for part in message.parts
    if part.kind == "tool_result" and part.metadata["tool_name"] == "mcp__github__get_issue"
)
assert tool_part.metadata["ok"] is False
assert tool_part.metadata["data"]["mcp_activation_required"] is True
assert session.pending_permission_execution is None
```

该测试必须证明拒绝发生在权限预检和 transport 调用之前。

- [ ] **Step 4: 写权限暂停恢复和流式路径测试**

在 `tests/test_app_runtime.py` 增加：搜索后调用一个 MCP 工具，权限确认暂停；`runner.resume_with_user_input(..., "allow_once")` 复用 `_pending_permission_loop` 后，下一次 provider request 仍包含该 MCP definition。再在 `tests/test_agent_context_loop.py` 用 `StreamingProvider` 跑同样的 search -> MCP tool -> final 链，断言同步和流式 definition 集合一致。

同时在现有 prompt-too-long retry fake 上增加一个已激活 MCP definition，断言失败重试前后都存在，不因 compact 重建 request 而丢失。

- [ ] **Step 5: 运行新测试，确认当前仍全量暴露且没有调用守卫**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_agent_context_loop.py -k "mcp_schema or searched_mcp or mcp_activation" -q
.venv/bin/python -m pytest \
  tests/test_app_runtime.py -k "mcp and permission" -q
```

Expected: FAIL；首次请求仍含全部 MCP definition，搜索结果不会改变后续 definition，猜测名称会进入现有权限/执行路径。

- [ ] **Step 6: 给 `ToolExecutor` 增加两个窄回调**

在 `firstcoder/agent/tool_execution.py` 构造参数增加：

```python
validate_tool_call: Callable[[ToolCall], ToolResult | None] | None = None,
observe_tool_result: Callable[[ToolCall, ToolResult], None] | None = None,
```

保存为 `_validate_tool_call`、`_observe_tool_result`。在 `execute_interactive()` 取得当前 `tool_call` 后、`HIDDEN_TOOL_STATUS_NAMES` 和 `_prepare_permission()` 之前调用 validator：

```python
validation_error = (
    self._validate_tool_call(tool_call)
    if self._validate_tool_call is not None
    else None
)
if validation_error is not None:
    self._emit_event("denied", tool_call, result=validation_error)
    self._record_result(tool_call, validation_error, state=state)
    index += 1
    continue
```

在 `_record_result()` 中先 `append_tool_result()`，随后调用 observer：

```python
self.session.append_tool_result(tool_call=tool_call, result=result)
if self._observe_tool_result is not None:
    self._observe_tool_result(tool_call, result)
```

observer 只影响下一次 provider request，不改变已写入 tool result。现有调用方不传回调时行为完全不变。

- [ ] **Step 7: 在 `AgentLoop` 维护回合内激活集合**

在 `AgentLoop.__init__()` 完成传入 tools 注册后，计算当前 registry 中所有 `mcp__` 名称，并初始化：

```python
self._mcp_tool_names = {
    name for name in self.session.tool_registry.names() if name.startswith("mcp__")
}
self._active_mcp_tool_names: set[str] = set()
```

把两个方法传给 `ToolExecutor`。新增：

```python
def _validate_mcp_tool_call(self, tool_call: ToolCall) -> ToolResult | None:
    if tool_call.name not in self._mcp_tool_names:
        return None
    if tool_call.name in self._active_mcp_tool_names:
        return None
    return make_error_result(
        tool_call.name,
        "MCP tool is not active for this user turn. Call mcp_tool_search first.",
        mcp_activation_required=True,
    )


def _observe_mcp_search_result(
    self,
    tool_call: ToolCall,
    result: ToolResult,
) -> None:
    if tool_call.name != "mcp_tool_search" or not result.ok:
        return
    payload = result.data.get("mcp_tool_search")
    if not isinstance(payload, dict):
        return
    activated = payload.get("activated_tools")
    if not isinstance(activated, list):
        return
    self._active_mcp_tool_names.update(
        name
        for name in activated
        if isinstance(name, str) and name in self._mcp_tool_names
    )
```

这里必须与 registry 交集，不能相信任意 tool result data 激活不存在或非 MCP 的工具。

在 `_begin_turn(new_user_turn=True)` 中最先执行 `self._active_mcp_tool_names.clear()`；`resume_with_user_input()` 调用 `_begin_turn(new_user_turn=False)`，因此权限恢复不清空。

- [ ] **Step 8: 只过滤 provider definitions，不注销 executor**

修改 `AgentLoop._provider_tool_definitions()`：

```python
definitions = []
for definition in self.session.tool_registry.definitions():
    if definition.name in HIDDEN_TOOL_STATUS_NAMES:
        continue
    if (
        definition.name in self._mcp_tool_names
        and definition.name not in self._active_mcp_tool_names
    ):
        continue
    definitions.append(self._augment_tool_definition(definition))
return definitions
```

不要从 `ToolRegistry` 删除 MCP 工具；搜索后的同回合调用、权限恢复和历史 replay 仍需要 executor。`mcp_tool_search` 不以 `mcp__` 开头，因此常驻。

- [ ] **Step 9: 验证激活后的真实调用仍走原精确权限**

在 `tests/test_mcp_integration.py` 扩展集成用例：先执行搜索并让 AgentLoop 激活目标，然后调用适配后的 MCP 工具，断言 pending permission 的 action 仍是 `mcp_tool`、target 仍是精确 `echo/echo`；批准后才调用 fake manager。不要为搜索工具创建 grant。

- [ ] **Step 10: 跑 AgentLoop、runtime、MCP 聚焦测试**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_agent_context_loop.py \
  tests/test_app_runtime.py \
  tests/test_mcp_integration.py \
  tests/test_permissions_policy.py \
  tests/test_permissions_manager.py -q
```

Expected: PASS；同步、流式、权限恢复、prompt-too-long retry 都遵守同一激活集合。

- [ ] **Step 11: 提交回合级暴露改动**

```sh
git add \
  firstcoder/agent/loop.py \
  firstcoder/agent/tool_execution.py \
  tests/test_agent_context_loop.py \
  tests/test_app_runtime.py \
  tests/test_mcp_integration.py
git commit -m "Defer MCP schemas by user turn"
```

---

### Task 5: 更新设计文档并完成全量验证

**Files:**

- Modify: `docs/SKILL_SYSTEM_DESIGN.zh-CN.md`
- Modify: `docs/SKILL_SYSTEM_DESIGN.md`
- Modify: `docs/MCP.zh-CN.md`
- Modify: `docs/MCP.md`
- Verify: `docs/superpowers/specs/2026-07-24-skill-mcp-progressive-disclosure-design.zh-CN.md`

- [ ] **Step 1: 更新 Skill 中英文事实说明**

将“按顺序只加入完整条目”的描述改为：

```text
目录最多 8,000 字符。渲染器先为每个有效 Skill 保留完整名称行，再把剩余预算公平分给单行 description；Skill 少时 description 仍可使用最多 240 字符。只有名称骨架本身超过总预算时才截断目录，并输出明确警告。完整正文始终通过 load_skill 按需加载。
```

排障项不再把“8,000 字符导致后半目录消失”描述成预期行为。

- [ ] **Step 2: 更新 MCP 中英文链路和生命周期**

把链路更新为：

```text
McpManager tools/list + enabled/allowed_tools filtering
  -> McpToolProvider registers ordinary MCP executors
  -> mcp_tool_search exposes compact local discovery
  -> AgentLoop sends no concrete MCP schema initially
  -> search result activates at most 8 schemas for this user turn
  -> ordinary PermissionAwareToolRegistry + exact MCP permission + call_tool
```

明确写出：

- MCP 连接和 executor 注册不等于 schema 常驻；
- 新用户消息清空激活集合；
- 权限确认恢复保留，普通 `ask_user` 回答按新消息清空；
- 未激活名称猜测在权限预检前被拒绝；
- `allowed_tools` 仍是硬过滤，不会被搜索绕过；
- 当前仍不支持 resources、prompts、sampling、roots、elicitation、OAuth 等非本次范围。

- [ ] **Step 3: 跑格式和占位检查**

Run:

```sh
git diff --check
rg -n "TBD|TODO|implement later|待定" \
  firstcoder/skills/catalog.py \
  firstcoder/mcp/search.py \
  docs/SKILL_SYSTEM_DESIGN.md \
  docs/SKILL_SYSTEM_DESIGN.zh-CN.md \
  docs/MCP.md \
  docs/MCP.zh-CN.md
```

Expected: `git diff --check` 无输出；`rg` 无命中。

- [ ] **Step 4: 跑全部相关聚焦测试**

Run:

```sh
.venv/bin/python -m pytest \
  tests/test_skill_discovery.py \
  tests/test_skill_loader.py \
  tests/test_agent_skill_flow.py \
  tests/test_mcp_search.py \
  tests/test_mcp_adapter.py \
  tests/test_mcp_manager.py \
  tests/test_mcp_config.py \
  tests/test_mcp_integration.py \
  tests/test_app_factory.py \
  tests/test_app_session_commands.py \
  tests/test_agent_context_loop.py \
  tests/test_app_runtime.py -q
```

Expected: PASS。

- [ ] **Step 5: 跑完整测试套件**

Run:

```sh
.venv/bin/python -m pytest
```

Expected: 全部 PASS；不得只根据聚焦测试声明完成。

- [ ] **Step 6: 用既有测试做实际请求面静态验收**

运行 Task 4 新增的具体测试（不要只观察日志）：

```sh
.venv/bin/python -m pytest \
  tests/test_agent_context_loop.py::test_agent_loop_exposes_only_searched_mcp_schemas_for_current_turn \
  tests/test_agent_context_loop.py::test_agent_loop_clears_mcp_activation_on_next_user_turn -q
```

这些测试必须输出/断言三次请求的 tool 名集合：

```text
request 1: builtins + mcp_tool_search, no mcp__*
request 2 after search: builtins + mcp_tool_search + selected mcp__* (<= 8)
request 1 of next user turn: builtins + mcp_tool_search, no mcp__*
```

同时运行 Task 1 的 `test_render_skill_catalog_keeps_every_skill_name_within_budget`，断言 `render_skill_catalog()` 中每个 resolved name 都出现且总长不超过 8,000。该验收只读用户现有 Skill 文件，不修改它们。

- [ ] **Step 7: 检查最终 diff 没有越界重构**

Run:

```sh
git diff --stat HEAD~4..HEAD
git diff -- \
  firstcoder/context \
  firstcoder/providers \
  firstcoder/permissions \
  firstcoder/mcp/transport.py \
  firstcoder/mcp/manager.py
```

Expected: 第二条无生产代码 diff；如果这些边界出现修改，必须说明设计中哪条验收无法在现有接口下完成，否则撤回越界改动。

- [ ] **Step 8: 提交文档与最终测试调整**

```sh
git add \
  docs/SKILL_SYSTEM_DESIGN.zh-CN.md \
  docs/SKILL_SYSTEM_DESIGN.md \
  docs/MCP.zh-CN.md \
  docs/MCP.md
git commit -m "Document progressive tool disclosure"
```

## 完成定义

只有以下条件全部满足才能宣布完成：

- 实际 resolved Skill catalog 的每个名称在 8,000 字符目录中可见，或仅在名称骨架本身超预算时出现明确截断警告。
- 未搜索时任何主请求都不包含具体 `mcp__*` definition。
- 单次搜索最多激活 8 个经过 `enabled`、`allowed_tools` 和 adapter 校验的工具。
- 未激活 MCP 猜测调用不会进入权限或 transport；激活后的真实调用仍走精确 MCP 权限。
- 同回合工具循环、权限恢复、streaming 和 prompt-too-long retry 保留激活集合；下一条新用户消息清空。
- 自定义 tools、无 MCP、连接失败、new/resume/fork 行为符合原有契约。
- 没有新增 session schema、第三方搜索依赖、provider 特判或 MCP transport 分支。
- 聚焦测试和完整 `.venv/bin/python -m pytest` 全部通过。
