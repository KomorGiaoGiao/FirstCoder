# Skill 系统设计

[English](SKILL_SYSTEM_DESIGN.md)

## Skill 是什么

Skill 是基于文件系统、可复用的模型工作流说明。它不是可执行 plugin，也不是 FirstCoder 在用户消息到来后自动选择的隐藏规则。FirstCoder 负责发现、安全索引和加载文件；LLM 根据精简目录决定是否调用 `load_skill`。

## 一轮 Skill 使用流程

```text
SessionBootstrap 发现项目与全局 skill
  -> 按 name 解析唯一有效项（项目优先于全局）
  -> system prompt 只放 name + 单行短 description
  -> 用户消息进入正常 agent loop
  -> LLM 判断需要某个 skill
  -> LLM 调用 load_skill(name, args?)
  -> SkillLoader 校验登记的 root-relative path 并读取 SKILL.md
  -> 成功后写 skill_selected / skill_loaded
  -> 完整正文作为普通 tool_result 进入 append-only session
  -> LLM 按正文调用 view/read_multi/shell 等现有工具继续工作
```

没有本地关键词路由，也没有 `session.loaded_skills` 常驻 system prompt。未调用 `load_skill` 时，skill 正文不会进入模型请求。

## 发现与有效目录

| 优先级 | 位置 | 来源 |
| ---: | --- | --- |
| 1 | `<project>/.agents/skills/*/SKILL.md` | 项目 agent skill |
| 2 | `<project>/skills/*.md` | 项目 markdown skill |
| 3 | `~/.agents/skills`、`~/.codex/skills`、`~/.firstcoder/skills` | 全局 agent/markdown skill |
| 4 | `FIRSTCODER_SKILL_ROOTS` 的逗号分隔目录 | 额外全局根 |

`<project>/skills/INDEX.md` 是目录说明，不是可调用 skill。设 `FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` 可关闭全局发现。

底层 discovery 保留来源记录，运行时再按 `name` 生成唯一有效目录。同名时按上表优先；同一优先级用稳定 root/path 顺序决定，确保 system prompt、TUI 和 `load_skill` 看到同一条定义。

## 模型可见目录

模型每轮只看到类似：

```text
- code-review: Review code correctness and maintainability.
- pdf: Read and transform PDF documents.
Use load_skill(name, args?) to load full instructions when needed.
```

目录不包含 filesystem path、root、source enum 或重复名称。description 会压成单行并限制长度；整个目录最多 8,000 字符，只加入完整条目，不输出半截记录。

## `load_skill` 工具

```text
load_skill(name: string, args?: string)
```

- `name` 必须精确匹配有效目录，不接受路径。
- `args` 只传递本次任务参数，不参与寻址。
- 实际 root/path 来自 discovery，并再次经过 `SkillLoader` 越界校验。
- 返回完整 `SKILL.md` 和其中解析出的 required-files 路径列表。
- required file 不会自动展开；模型按 skill 指令用现有读取工具按需访问。
- 未知名称、文件消失或读取失败返回普通工具错误，不写成功审计事件。

`load_skill` 是 session-scoped 保留工具，外部 supplied tool 不能覆盖它。

## 审计、Resume 与压缩

成功加载写入 `skill_selected` 和 `skill_loaded`。完整正文同时作为正常 `tool_result` 事件落入 JSONL，因此：

- resume 重放当时真实返回的正文，不重新读取当前磁盘文件；
- provider 看到标准的 assistant tool-call -> tool-result 序列；
- checkpoint 和工具结果归档可以按通用规则处理它；
- 再次调用同一 skill 会产生新的明确事实，不依赖隐藏的常驻集合。

旧会话中的 skill 审计事件继续保留，但不会被恢复成 system prompt 内容。

## TUI 命令

- `/skills`：打开去重后的有效 skill 选择器。
- `/skill <name>`：查看内部详情，包括路径和来源；详情不会进入 system prompt。
- `/skill-use <name>`：在输入框生成一条明确要求模型调用 `load_skill` 的指令。
- `/<skill-name> <instruction>`：把 instruction 作为 args，引导模型先调用 `load_skill`。

命令不会直接读取 skill 或修改 session 状态，正式加载始终经过模型工具调用这一条路径。

## 新增项目 Skill

1. 结构化工作流放 `<project>/.agents/skills/<name>/SKILL.md`；简单工作流也可放 `<project>/skills/<name>.md`。
2. frontmatter 提供简短、可区分的 `name` 和 `description`。
3. 在正文中说明何时使用、步骤、验证标准，以及按需读取的 references/scripts/assets。
4. 不依赖 Python 关键词触发；description 是 LLM 选择 skill 的主要索引。
5. 测试 discovery、同名优先级、`load_skill` 成功/失败、工具结果 resume 和路径越界拒绝。

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_skill_loader.py \
  tests/test_agent_skill_flow.py tests/test_context_system_prompt.py -q
```

## 排障

| 现象 | 检查 |
| --- | --- |
| 模型看不到 skill | 目录布局、disable-global flag、frontmatter name/description、8,000 字符目录预算 |
| 同名 skill 选错 | 项目/全局来源优先级和稳定 root/path 排序 |
| 模型没有调用 skill | system prompt 目录是否有清晰 description，或使用 `/<skill-name>` 明确指定 |
| `load_skill` 失败 | 名称是否精确、文件是否仍存在、登记 path 是否在 root 内 |
| resume 后正文变化 | 检查历史 tool result；resume 不会重新读取磁盘 skill |

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)、[代码阅读指南](CODEBASE_READING_GUIDE.zh-CN.md)。
