# Skill 全量发现与 MCP 工具按需暴露设计

**日期：** 2026-07-24  
**状态：** 已确认设计，尚未实现  
**范围：** Skill 模型可见目录预算、MCP 工具 schema 暴露策略

## 1. 背景

FirstCoder 当前已经有两条可运行链路：

- Skill 在启动时被发现并去重，模型从 system prompt 中的精简目录判断是否调用 `load_skill`；只有调用后，完整 `SKILL.md` 才作为普通 tool result 进入上下文。
- MCP 服务器在启动时连接并发现工具，`McpToolProvider` 把每个 MCP 工具适配成普通 `Tool`，随后与内置工具一起注册并在每次主模型请求的 `ChatRequest.tools` 中全量发送。

这两条链路的问题不同：

1. Skill 的正文已经按需加载，但目录使用固定 8,000 字符预算并按稳定顺序逐条准入。前面的长 description 填满预算后，后面的 Skill 会完全不可见。
2. MCP 的工具 schema 没有按需暴露。大型服务器（尤其 GitHub）即使当前任务不需要，也会在每次请求中贡献大量 Fixed tokens。

本设计只修这两个暴露策略，不重构 Skill、MCP、权限、会话或上下文系统。

## 2. 目标

### 2.1 Skill

- 每个解析后的唯一 Skill 至少以名称出现在模型可见目录中。
- 在 8,000 字符总预算内公平缩短 description，避免按排序位置让后半目录完全消失。
- 保持 `load_skill(name, args?)` 是加载完整正文的唯一模型路径。

### 2.2 MCP

- MCP 服务器仍然正常连接，并在进程内保留经过 `enabled`、`allowed_tools` 过滤后的完整工具目录。
- 主模型默认只看到内置工具和一个常驻的 `mcp_tool_search`，不再看到全部 MCP schema。
- 模型调用 `mcp_tool_search` 后，只把少量匹配工具的 schema 暴露到当前用户回合后续请求。
- 搜索命中的工具仍走现有权限确认和 `McpManager.call_tool` 执行链。

## 3. 非目标

本次不做以下事项：

- 不引入 embedding、向量数据库、外部搜索服务或 LLM 二次路由。
- 不把 Skill 和 MCP 合并为统一 plugin 框架。
- 不修改 MCP transport、连接重试、OAuth、权限 grant 或工具结果协议。
- 不把 MCP 激活集合写入 JSONL，不提升 session schema 版本。
- 不把已加载 Skill 永久追加到 system prompt。
- 不修改 checkpoint、归档、压缩阈值或 provider 协议。
- 不增加服务端 `defer_loading` 等只适用于特定 provider 的协议分支。

## 4. 方案选择

### 4.1 Skill 备选方案

#### 方案 A：保持顺序准入

实现零成本，但后半目录继续完全不可见，不满足目标。

#### 方案 B：所有名称优先，剩余预算公平分配 description（采用）

继续使用现有目录和 `load_skill`，只改预算算法。改动最小，且能保证所有 Skill 被模型感知。

#### 方案 C：增加 `skill_search`

可以进一步缩小初始目录，但 80 个左右的 Skill 没有必要引入搜索状态、排名质量和额外工具轮次。

### 4.2 MCP 备选方案

#### 方案 A：只依赖 `allowed_tools`

能够立即减少工具数，但需要用户手工维护白名单，无法随着不同任务自动选择。

#### 方案 B：本地工具搜索加回合内 schema 激活（采用）

保留完整本地目录，使用确定性文本评分选出少量工具，并只在当前用户回合暴露。无需修改 transport 和权限边界。

#### 方案 C：重构成 provider 原生延迟工具协议

不同 provider 支持程度不一致，会扩大适配层和兼容范围，不适合这次最小改造。

## 5. Skill 目录设计

### 5.1 保持现有解析规则

`resolve_skill_catalog()` 继续负责：

- 按来源优先级选择同名 Skill；
- 项目 Skill 优先于全局 Skill；
- 同优先级按稳定 root/path 顺序决胜；
- 最终按名称排序。

本次不改变发现和覆盖语义。

### 5.2 两阶段预算

`render_skill_catalog()` 改为两阶段计算：

1. 先预留结尾的 `SKILL_LOAD_INSTRUCTION`。
2. 为每个 Skill 预留 `- <name>:` 行骨架和换行符，保证所有名称优先进入目录。
3. 用剩余字符数除以 Skill 数量，得到本次目录的公平 description 上限。
4. 每个 description 仍先折叠空白，并受既有 `SKILL_DESCRIPTION_MAX_CHARS = 240` 上限约束。
5. description 超出动态上限时使用 `...` 截断；预算太小时输出纯名称行，不伪造 description。

常见情况下目录形态为：

```text
- family-office-research: Generate comprehensive family-office research...
- gh-fix-ci: Inspect and fix failing GitHub Actions...
- lark-doc: 飞书云文档操作...
Use load_skill(name, args?) to load full instructions when needed.
```

### 5.3 极端溢出

如果 Skill 名称和固定格式本身已经超过 8,000 字符，则任何算法都无法在同一硬预算内完整渲染所有名称。此时渲染器必须确定性地：

- 只输出能够完整容纳的名称行；
- 不输出半截名称；
- 在结尾保留加载指令，并增加明确的目录截断提示；
- 不退回当前“长 description 抢占后续名称”的策略。

当前实际规模不会触发该极端路径，但测试需要固定其行为。

## 6. MCP 按需暴露设计

### 6.1 三个集合

运行时区分三类工具：

1. **基础工具集：** 内置工具、session-scoped 工具和其他显式 supplied tools，继续按当前方式常驻。
2. **MCP 目录：** `McpManager.tools()` 返回的完整、已过滤工具描述，只保存在 FirstCoder 进程内。
3. **回合激活集：** 当前用户回合内由搜索命中的 MCP 工具名称集合。

MCP 工具 executor 仍然注册在 `ToolRegistry` 中，保证模型命中后可以走原执行链；是否把其 definition 放进 `ChatRequest.tools`，由请求构建阶段单独决定。这样不需要增加第二套执行注册表。

### 6.2 常驻搜索工具

仅当以下条件同时满足时注册 `mcp_tool_search`：

- 当前会话使用默认工具装配，而不是调用方显式传入自定义 `tools`；
- 至少有一个已连接且通过配置过滤的 MCP 工具。

工具输入 schema：

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "Describe the external capability or MCP operation needed."
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

搜索工具只查询本地目录，不访问网络、不调用 MCP 工具，也不触发 MCP 工具权限确认。

成功结果包含：

- 命中的模型可见工具名，例如 `mcp__github__get_pull_request`；
- server/tool 原始名称；
- 简短 description；
- 本次新激活数量。

无匹配时返回成功的空结果和简短提示，允许模型改写 query；目录读取失败则返回安全错误，不泄露 MCP 配置或认证信息。

### 6.3 确定性搜索

第一版使用本地、无依赖的文本评分。每个候选的搜索文本由以下字段组成：

- server 名称；
- tool 名称，同时保留原始下划线形式和按 `_`、`-` 拆分后的词；
- description。

query 和候选都做 Unicode `casefold()`、空白归一化和基础 token 拆分。排序规则按优先级依次为：

1. 完整 tool 名称精确匹配；
2. query token 命中 tool 名称；
3. query token 命中 server 名称；
4. query token 命中 description；
5. 同分时按模型可见工具名稳定排序。

默认最多返回并激活 8 个工具。第一版不向模型开放 `limit` 参数，避免模型一次请求把整个目录重新激活。

### 6.4 回合生命周期

“当前用户回合”从一条新的用户消息进入 `run_user_turn_interactive()` 开始，到该消息对应的最终回答结束。

生命周期规则：

- 新用户消息开始前清空上一回合 MCP 激活集。
- 当前回合调用 `mcp_tool_search` 后，把命中名称加入激活集。
- 同一回合的后续模型工具循环继续暴露这些 schema。
- 权限确认暂停和对应 resume 不清空激活集，因为运行时会复用同一个暂停的 `AgentLoop`。
- 普通 `ask_user` 当前没有同回合 resume 协议；用户回答会作为下一条新用户消息进入，因此按新回合清空，模型需要时重新执行一次本地搜索。本次不顺手重构 `ask_user` 语义。
- prompt-too-long compact 后的同回合重试不清空激活集。
- 用户发送下一条新消息时清空，即使语义上仍在继续上一话题；模型可以用一次本地搜索重新激活。
- 会话 resume 后没有持久化激活集；第一条新用户消息从空集合开始。

这个边界保证 Fixed tokens 不会随着长会话单调增长。

### 6.5 请求构建

`AgentLoop._provider_tool_definitions()` 继续先检查 provider 是否支持 tools，然后：

- 保留所有非 MCP definition；
- 保留 `mcp_tool_search`；
- MCP definition 只在名称位于当前回合激活集时加入；
- 最后继续应用现有后台工具 schema 增强和隐藏工具过滤。

模型在首次搜索请求中看不到具体 MCP schema。搜索 tool result 写入普通历史后，下一次 provider 请求同时包含：

- `mcp_tool_search` 的 tool call/result；
- 新激活工具的 schema。

模型随后可正常调用命中的 MCP 工具。

## 7. 权限与安全边界

本次只改变 definition 的模型可见性，不改变执行授权：

- `mcp_tool_search` 是只读本地目录操作，不需要 MCP 工具权限。
- 被激活的 MCP 工具仍由 `adapt_mcp_tool()` 创建，保留 `PermissionAction.MCP_TOOL`、精确的 `<server>/<tool>` target 和 `allow_auto=False`。
- 搜索结果本身不是可执行句柄，模型必须在下一次响应中发出正常 tool call。
- `allowed_tools` 仍在 `McpManager` 保存目录前过滤，因此搜索不可能重新找回配置禁止的工具。
- 工具名冲突和非法名称仍沿用适配器现有拒绝规则。

## 8. 会话、恢复与上下文

- 不新增事件类型；搜索调用和结果按普通 tool call/result 保存。
- 激活集合不持久化，不参与 replay、fork 或 checkpoint。
- 历史 MCP tool call/result 即使对应 schema 当前未激活，也继续由现有 `ContextBuilder` 投影和压缩。
- provider 请求必须保持合法的历史 tool call/result 配对；definition 是否继续暴露不影响已完成历史消息。
- `/context` 的 Fixed 统计会自然反映当前请求实际暴露的 schema 数量，不另造估算口径。

## 9. 装配与兼容

### 9.1 默认工具模式

`McpToolProvider` 继续把发现到的 MCP 工具适配并注册，使 executor、权限和名称解析保持原状；同时向会话/循环提供 MCP 工具名称集合，用于 definition 过滤和搜索激活。

### 9.2 自定义工具模式

调用方显式传入 `tools` 时，维持当前语义：不自动追加 MCP 工具，也不注册 `mcp_tool_search`。

### 9.3 无 MCP 与连接失败

没有可用 MCP 工具时：

- 不注册 `mcp_tool_search`；
- 基础工具照常工作；
- `/mcp` 现有状态和错误显示不变。

### 9.4 配置

第一版不增加配置字段。现有配置继续负责：

- `enabled`：是否连接服务器；
- `allowed_tools`：进入本地可搜索目录的第一层白名单。

每次搜索最多 8 个作为代码常量，待真实使用数据证明需要配置后再开放。

## 10. 组件边界

计划中的生产代码改动应保持以下职责：

- `firstcoder/skills/catalog.py`：只负责 Skill 解析和模型可见目录预算。
- `firstcoder/mcp/search.py`：新增纯搜索/评分逻辑和 `mcp_tool_search` 工具构造，不负责 transport 或权限。
- `firstcoder/app/factory.py`：装配完整 MCP executor 集和搜索工具，并把 MCP 名称集合交给会话运行时。
- `firstcoder/agent/loop.py`：维护回合内激活集合，按集合过滤 provider-visible definitions。
- `firstcoder/mcp/manager.py`、`firstcoder/mcp/adapter.py`：原则上保持现有连接、过滤、适配和执行语义；只在必要时增加窄接口，不搬迁职责。

不新增通用 capability registry、plugin manager 或 provider 专用 deferred-tool 抽象。

## 11. 测试策略

### 11.1 Skill

- 100 个长 description 的 Skill 在目录预算内仍能看到全部名称。
- 目录总长度不超过 8,000 字符。
- Skill 少时仍可使用最多 240 字符 description，不被无意义压短。
- description 空白归一化、截断后缀和同名优先级不变。
- 极端名称预算溢出时只输出完整行，并有明确截断提示。

### 11.2 MCP 搜索

- 精确 tool name、拆词、server 和 description 命中具有确定性排序。
- 同分结果按模型可见名称稳定排序。
- 最多返回 8 个，且只从 `allowed_tools` 过滤后的目录搜索。
- 无匹配、空 query 和目录异常返回安全结果。

### 11.3 AgentLoop 生命周期

- 当前回合首次请求只有基础工具和 `mcp_tool_search`。
- 搜索结果进入历史后，下一次请求出现命中的 MCP schema。
- 同回合多轮调用、权限暂停恢复和 prompt-too-long 重试保留激活集合；普通 `ask_user` 回答作为新用户回合清空。
- 下一条新用户消息清空上一回合集合。
- 未激活 MCP 工具即使 executor 已注册，也不进入 provider definitions。
- 实际调用激活工具仍触发现有精确 MCP 权限确认。

### 11.4 装配与回归

- 默认模式连接一次并注册搜索能力。
- 自定义工具模式不追加 MCP 或搜索工具。
- MCP 连接失败时基础工具不受影响。
- OpenAI-compatible 和 Anthropic provider 都只收到筛选后的 `ChatRequest.tools`。
- 完整测试套件通过。

## 12. 验收标准

实现完成后必须同时满足：

1. 当前实际解析出的所有 Skill 名称都在模型可见目录中，且目录不超过既有预算。
2. 未进行 MCP 搜索时，主模型请求不含具体 MCP 工具 schema。
3. 搜索后只有最多 8 个命中工具在当前用户回合可见和可调用。
4. 下一条用户消息的首次请求不携带上一回合激活的 MCP schema。
5. `allowed_tools`、MCP 精确权限确认、tool result 持久化和上下文压缩行为保持不变。
6. 不新增 session schema，不引入第三方搜索依赖，不增加 provider 特判。
7. 聚焦测试及完整 pytest 全部通过。
