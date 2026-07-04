# Skill 系统实施方案

> **给后续 agent 的要求：** 这份方案必须按 TDD 实施。不要用“把 skill 文件全文塞进 prompt”来冒充 skill 系统。目标是把 skill 做成 FirstCoder 的一等能力：可发现、可路由、可加载、可审计、可真实验证。

**目标：** 让 FirstCoder 在动手前可靠地识别并遵循项目 skill / 用户 skill，并且能从会话日志里看到“选中了哪个 skill、加载了哪个 skill、是否按 skill 要求继续读了必要文件”的证据。

**核心架构：** 在项目规则和 agent loop 中间新增 `firstcoder.skills` 层。这个层负责发现 skill catalog、对每轮用户请求做路由、执行 skill 加载协议、把加载事实写进 session/runtime state，并把紧凑的 skill 上下文暴露给模型。完整 skill 文件只在被选中时加载，不应该每轮无脑注入全部 skill。

**主要验收方向：** 在 `/Users/x/Desktop/资讯数据库` 中，用户说“按框架跑一次今天的全球家办资讯简报”时，FirstCoder 必须识别到 `skills/global-family-office-news-brief.md`，在实质工作前加载它，并继续处理该 skill 声明的必读文件。代理不能直接跳去旧 pipeline，也不能在没有 `skill_loaded` 证据时产出正式报告。

---

## 非目标

- 不要把 `/Users/x/Desktop/资讯数据库` 或家办 skill 名称硬编码进 FirstCoder。
- 不要每轮把所有 skill 正文都塞进系统提示词。
- 不要把“AGENTS.md 被放进 prompt”当成 skill 系统已经完成。
- 不要求联网或真实 API 才能测试 skill 系统。
- 不要让 skill 只是一段模型约定，必须有 session 事件可审计。

## Skill 来源

第一版就应该支持两类来源：项目级 skill 和机器全局 skill。

- 项目 markdown skills：`<project_root>/skills/*.md`，可选 `<project_root>/skills/INDEX.md`。
- 项目 agent skills：`<project_root>/.agents/skills/*/SKILL.md`。
- 机器全局 agent skills：默认扫描 `~/.agents/skills/*/SKILL.md` 和 `~/.codex/skills/*/SKILL.md`。
- 机器全局 markdown skills：可选扫描 `~/.firstcoder/skills/*.md`，以及配置项指定的额外目录。

全局 skill 的定位：

- 全局 skill 是这台机器上的长期能力库，例如邮件、飞书、图片生成、内容工作流、研究框架等。
- 项目 skill 是当前仓库的局部工作流和规则。
- 两者都进入同一个 `SkillCatalog`，但必须保留 `source` 和 `root`，方便解释“这个 skill 从哪里来”。

需要解析的元数据：

- `name`：来自 frontmatter `name`、一级标题、目录名或文件名。
- `description`：来自 frontmatter `description`、INDEX 表格行、一级标题或首个非空行。
- `path`：项目 skill 使用项目相对路径；全局 skill 使用 root 相对路径，并保留 root。
- `source`：`project_markdown`、`project_agent_skill`、`global_markdown`、`global_agent_skill`。
- `root`：skill 所属根目录，用于加载和审计。
- `triggers`：优先来自 frontmatter；没有时从 description、INDEX 行和文件名推断。
- `required_files`：加载 skill 后，从“必读文件 / must read / required files”等段落里解析出的相对路径。

## Skill 优先级和冲突

同名或同触发词 skill 可能同时存在，必须有明确规则：

1. 用户显式指定路径时，路径优先。
2. 项目级 skill 优先于全局 skill。
3. `.agents/skills/*/SKILL.md` 这种带 frontmatter 的 skill 优先于同名普通 markdown skill。
4. 如果项目 skill 和全局 skill 都强匹配，但不是同一语义，返回 `ambiguous`，不要静默选择。
5. final answer 或日志必须展示使用的是项目 skill 还是全局 skill，例如 `project:skills/foo.md` 或 `global:~/.agents/skills/foo/SKILL.md`。

全局 skill 不能覆盖项目约束。项目 `AGENTS.md`、项目权限策略和当前 workspace 边界始终优先。

## 核心行为契约

每一轮用户请求都应遵守：

1. 从当前项目发现并构建紧凑的 skill catalog。
2. 用用户消息、`AGENTS.md`、`skills/INDEX.md`、skill metadata 和用户显式提及做路由。
3. 如果路由结果是高置信 skill，必须在实质工作前加载该 skill。
4. 加载 skill 意味着读取完整 skill 文件，并写入 session 事件。
5. 如果 skill 声明了必读文件，必须在依赖这些文件的流程步骤前读取它们。
6. final answer 和 UI/display log 不能声称使用了某个 skill，除非 session 里有对应 `skill_loaded` 事件。
7. resume 后必须从 session 事件恢复已加载 skill 状态，同时从磁盘重新发现当前 skill catalog。
8. 全局 skill 可以补充能力，但不能绕过项目 `AGENTS.md`、权限策略或 sandbox 边界。

## 建议模块

- `firstcoder/skills/models.py`
  - `SkillSource`
  - `SkillDefinition`
  - `SkillCatalog`
  - `SkillRoute`
  - `LoadedSkill`
  - `SkillRoutingDecision`

- `firstcoder/skills/discovery.py`
  - `discover_project_skills(project_root) -> SkillCatalog`
  - `discover_global_skills(config) -> SkillCatalog`
  - `discover_all_skills(project_root, config) -> SkillCatalog`
  - 解析 `skills/*.md` 和 `.agents/skills/*/SKILL.md`。
  - 解析机器级 `~/.agents/skills/*/SKILL.md`、`~/.codex/skills/*/SKILL.md` 和配置的额外 roots。
  - 解析 `skills/INDEX.md` 作为路由上下文，而不是把它当成唯一真相。

- `firstcoder/skills/router.py`
  - `SkillRouter.route(user_message, agents_md, catalog) -> SkillRoutingDecision`
  - 第一版先做确定性文本路由。
  - 后续可以加 model-assisted routing，但单元测试不能依赖真实模型。

- `firstcoder/skills/loader.py`
  - `SkillLoader.load(skill) -> LoadedSkill`
  - 读取完整 skill 文件。
  - 从简单格式中抽取必读相对路径。

- `firstcoder/skills/protocol.py`
  - 格式化 skill protocol prompt。
  - 格式化紧凑 catalog。
  - 定义模型什么时候必须加载 skill、什么时候可以不加载。

- `firstcoder/skills/session.py`
  - 追加 `skill_selected`、`skill_loaded`、`skill_required_file_loaded` 事件。
  - replay 时恢复 loaded skill 状态。

## 集成点

- `AgentSession.from_project`
  - 根据 project root 发现项目 skill catalog。
  - 根据用户配置发现机器全局 skill catalog。
  - 合并 catalog，并保留来源和优先级。
  - 保存紧凑 catalog / protocol 到运行期 prompt 输入。

- `AgentSession.resume`
  - 通过 `ResumeService` 重新发现当前项目 skill catalog。
  - 重新发现机器全局 skill catalog，因为全局 skill 可能在 session 期间安装或更新。
  - 从 session log replay `skill_loaded` 等事件。

- `SystemPromptInputs`
  - 增加 `skill_protocol` 和 `skill_catalog_summary`。
  - fingerprint 必须包含 catalog summary hash；skill index 或 metadata 变化时 prompt cache 应失效。

- `AgentLoop`
  - 每轮新用户消息、第一次 provider call 前，先做 skill route。
  - 如果高置信选中 skill，可以二选一：
    - 代码自动加载 skill，并把加载内容注入下一次 provider request。
    - 或要求模型先调用专门的 `load_skill` 工具。
  - MVP 建议：高置信确定性路由由代码自动加载。这样合规性最强，也最容易测试。

- 工具层
  - 可选增加 `load_skill` 工具，服务低置信、模型主动选择或交互式探索场景。
  - 该工具接受 skill id 或 path，支持项目相对路径和已发现的全局 skill path。
  - 读取文件时必须限制在已发现 skill root 内，不能借 `load_skill` 任意读系统文件。
  - 读取后记录 `skill_loaded`，并把正文返回给模型。

## 配置入口

新增 FirstCoder 配置项：

```text
FIRSTCODER_SKILL_ROOTS
FIRSTCODER_DISABLE_GLOBAL_SKILLS
```

语义：

- `FIRSTCODER_SKILL_ROOTS` 是逗号分隔的额外全局 skill root。
- 默认值为空，但 FirstCoder 仍扫描内置默认根：`~/.agents/skills` 和 `~/.codex/skills`。
- `FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` 时，不加载任何机器全局 skill，只使用项目 skill。

TUI/CLI 后续可以增加：

```bash
firstcoder skills list
firstcoder skills doctor
```

第一版不一定要做完整 CLI，但 discovery 层要为它预留结构化返回。

## Prompt 形态

系统提示词应该包含三块：

1. **Skill protocol**
   - skill 是被路由后的强制工作流指令。
   - 被选中后必须加载完整文件，再做实质工作。
   - 必须遵守 required files、命令、质量门和输出契约。
   - 没有 `skill_loaded` 证据时，不得声称用了 skill。
   - 项目 skill 优先于全局 skill；全局 skill 不能覆盖项目约束。

2. **Available skills**
   - 只放紧凑目录：name、path、description、source、root。
   - 可放截断后的 INDEX 摘要。
   - 不放所有 skill 全文。

3. **Loaded skills this turn**
   - 只放本轮已选中 skill 的完整内容。
   - 如果解析到 required files，显示读取状态。

## 路由置信度

第一版确定性 router 使用这些 reason：

- `explicit`：用户明确提到 skill 名、skill path，或说“用这个 skill”。
- `agents_route`：`AGENTS.md` 中的表格或规则把用户意图映射到 skill path。
- `metadata_match`：用户消息和 skill name / description / triggers 有明显重合。
- `ambiguous`：多个 skill 都可能适用。
- `none`：没有必要加载 skill。

规则：

- `explicit` 和强 `agents_route` 应自动加载。
- 项目级 `agents_route` 优先于全局 `metadata_match`。
- `ambiguous` 不要沉默地乱选；应把候选暴露给模型，必要时让模型读 index 或问用户。
- `none` 不加载 skill，避免无意义流程化。

## Session 事件

必须用 append-only 事件记录 skill 行为，而不是只放在内存里：

```json
{"type":"skill_selected","turn":3,"skill_path":"skills/global-family-office-news-brief.md","reason":"agents_route","confidence":"high"}
{"type":"skill_loaded","turn":3,"skill_path":"skills/global-family-office-news-brief.md","content_hash":"...","bytes":16233}
{"type":"skill_required_file_loaded","turn":3,"skill_path":"skills/global-family-office-news-brief.md","file_path":"docs/evidence-policy.md","content_hash":"..."}
{"type":"skill_loaded","turn":4,"skill_scope":"global","skill_root":"~/.agents/skills","skill_path":"fetch-tweet/SKILL.md","content_hash":"...","bytes":2775}
```

这样 `.firstcoder` 日志可以被监听，测试也能不用真实模型就验证 skill 是否真的执行。

## 测试设计

### Discovery

- 能发现 `skills/*.md`。
- `skills/INDEX.md` 作为 index，不作为普通 skill body。
- 能发现 `.agents/skills/*/SKILL.md` 并解析 frontmatter。
- 能发现机器全局 `~/.agents/skills/*/SKILL.md` 和 `~/.codex/skills/*/SKILL.md`。
- `FIRSTCODER_SKILL_ROOTS` 能追加额外全局 root。
- `FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` 时不加载全局 skill。
- 产出项目相对路径。
- skill index 或 metadata 变化时，catalog fingerprint 变化。

### Routing

- 用户显式提到 skill 名或 path 时，选中该 skill。
- `AGENTS.md` 中类似“今天全球家办资讯 -> skills/global-family-office-news-brief.md”的路由能选中目标 skill。
- 敏感 claim / 诉讼 / 丑闻类措辞能路由到敏感复核 skill。
- 多个候选接近时返回 ambiguous，不要静默选错。
- 普通代码任务不应加载无关 skill。
- 项目 skill 和全局 skill 同名时，项目 skill 优先。
- 用户显式指定全局 skill path 时，可以选中全局 skill，但仍受项目规则约束。

### Loading

- 加载读取完整 skill 文件。
- 从“必读文件 / required files / must read”段落提取简单相对路径。
- skill path 缺失时返回结构化错误，不直接崩溃。
- 加载后写入 `skill_loaded`，包含 content hash 和字节数。

### Agent 集成

- 高置信路由任务第一次 provider request 前，session 已有 `skill_selected` 和 `skill_loaded`。
- 第一条 provider request 包含已加载 skill 正文。
- 未选中的 skill 正文不应被注入 prompt。
- resume 后能恢复 loaded skill 证据，同时重新发现当前 catalog。
- skill catalog 变化时 prompt cache 失效。

### 行为验收

构造一个临时项目，模拟资讯数据库结构：

```text
AGENTS.md
skills/INDEX.md
skills/global-family-office-news-brief.md
docs/evidence-policy.md
tools/registry.yaml
configs/source_weights.yaml
configs/discovery_queries.yaml
```

用 fake provider 断言：

- 用户说：“按框架跑一次今天的全球家办资讯简报”。
- provider call 之前，session 已经有 `skill_selected` 和 `skill_loaded`。
- provider system prompt 包含 `global-family-office-news-brief.md` 正文。
- provider system prompt 不包含无关 skill 正文。
- provider system prompt 的 skill catalog 可以同时列出项目 skill 和匹配的全局 skill，但只注入已选中 skill 正文。
- 如果 fake provider 试图在 required files 缺失时直接 final answer，系统能暴露缺失证据或阻止正式完成。

## 真实监听计划

单元测试通过后，用 `/Users/x/Desktop/资讯数据库` 做受控 dry-run：

1. 用 project root `/Users/x/Desktop/资讯数据库` 创建 FirstCoder session。
2. 发送：“按框架跑一次今天的全球家办资讯简报，先不要联网，只告诉我你会按哪个框架走”。
3. 检查：
   - `.firstcoder` session events 是否有 `skill_selected` 和 `skill_loaded`。
   - provider request 的 system prompt 是否包含选中 skill 正文。
   - UI/display/log 是否显示加载了哪个 skill。
4. 再跑一个敏感 claim 任务，验证它路由到 `skills/sensitive-claim-review.md`。
5. 再跑一个机器全局 skill 任务，例如给出 X/Twitter URL，验证它能发现并加载机器上的 `fetch-tweet` 全局 skill，同时不影响资讯数据库项目规则。

这只是验收方向，不是硬编码实现路径。

## 实施任务

### Task 1：Skill 模型和发现

文件：

- 新建 `firstcoder/skills/models.py`
- 新建 `firstcoder/skills/discovery.py`
- 新建 `tests/test_skill_discovery.py`

步骤：

- [ ] 写失败测试：项目 markdown skill、项目 `.agents/skills/*/SKILL.md`、机器全局 `~/.agents/skills/*/SKILL.md`、机器全局 `~/.codex/skills/*/SKILL.md`、INDEX 解析。
- [ ] 写失败测试：额外 root 配置和禁用全局 skill。
- [ ] 实现 metadata 解析和 catalog fingerprint。
- [ ] 跑 focused discovery tests。

### Task 2：确定性路由器

文件：

- 新建 `firstcoder/skills/router.py`
- 新建 `tests/test_skill_router.py`

步骤：

- [ ] 写失败测试：explicit、AGENTS-routed、metadata、ambiguous、none。
- [ ] 写失败测试：项目 skill 优先于同名全局 skill。
- [ ] 实现 lexical router 和置信度模型。
- [ ] 跑 focused routing tests。

### Task 3：Loader 和 session 事件

文件：

- 新建 `firstcoder/skills/loader.py`
- 扩展 session writer/store 事件处理。
- 新建 `tests/test_skill_loader.py`
- 必要时补 session replay 测试。

步骤：

- [ ] 写失败测试：完整加载、required files 提取、缺失路径、事件写入。
- [ ] 实现 loader 和事件 append/replay helper。
- [ ] 跑 focused loader/session tests。

### Task 4：Prompt protocol 集成

文件：

- 扩展 `SystemPromptInputs`
- 扩展 `SystemPromptBuilder`
- 新增 protocol formatting helper。
- 修改 `tests/test_context_system_prompt.py`

步骤：

- [ ] 写失败测试：catalog summary 出现、loaded skill 出现、无关 skill body 不出现、fingerprint 变化。
- [ ] 实现 prompt protocol 和 cache 集成。
- [ ] 跑 focused prompt tests。

### Task 5：Agent turn 集成

文件：

- 扩展 `AgentSession.from_project` / resume path。
- 扩展 `AgentLoop` provider call 前的准备阶段。
- 可选新增 `load_skill` tool。
- 新建 `tests/test_agent_skill_flow.py`

步骤：

- [ ] 写失败测试：高置信 skill 在 provider call 前加载。
- [ ] 写失败测试：ambiguous route 不乱选。
- [ ] 写失败测试：全局 skill 高置信匹配时能在 provider call 前加载。
- [ ] 实现 pre-provider routing/loading。
- [ ] 跑 agent skill flow tests。

### Task 6：资讯数据库验收运行

文件：

- 可新增 dry-run integration fixture。
- 可新增 `docs/SKILL_SYSTEM_RUNBOOK.md`。

步骤：

- [ ] 对 `/Users/x/Desktop/资讯数据库` 做受控 dry-run。
- [ ] 验证 skill events 和 provider request 内容。
- [ ] 验证无关 skill 正文没有被注入。
- [ ] 验证机器全局 skill 能被发现和加载。
- [ ] 记录剩余限制。

## 待定设计问题

- 高置信 skill 应该由代码自动加载，还是强制模型调用 `load_skill`？自动加载合规性更强；工具调用更透明。
- 已加载 skill 内容应该作为 system context、developer-style context，还是 synthetic tool result？当前 provider 抽象只有 system/user/assistant/tool，MVP 更适合 system context。
- required files 要多严格？MVP 可以记录和提示；后续可在缺失时阻止 final answer。
- 默认扫描 `~/.codex/skills` 是否会引入过多技能？如果 catalog 太大，可能需要目录白名单、top-N 摘要或 lazy catalog。
- 全局 skill 来源是否需要 trust policy？MVP 可只扫描用户 home 下默认目录，后续加签名、锁文件或启用清单。
- skill routing 和 `task_boundary` 谁先？skill routing 应在实质 provider work 前完成，但 `task_boundary` 仍需要当前 user message id。

## MVP 完成标准

MVP 完成必须同时满足：

- discovery、routing、loading、prompt integration、session events 都有测试。
- 高置信路由 skill 会在 provider call 前加载。
- loaded skill 证据能在 session log 中看到。
- resume 后仍能工作，并能看到当前项目 skill catalog 和机器全局 skill catalog。
- 机器全局 skill 能被发现、路由、加载，并且与项目 skill 冲突时遵守优先级。
- 资讯数据库验收场景能选中并加载预期 skill，且没有硬编码项目逻辑。
- focused tests 和相关 broader tests 通过；如果有无关既有失败，必须单独说明。
