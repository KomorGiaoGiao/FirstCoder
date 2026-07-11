# Skill 系统设计

[English](SKILL_SYSTEM_DESIGN.md)

## Skill 是什么

Skill 是基于文件系统、可复用的指令工作流。它不是可执行 plugin，也不是一段没留痕就硬塞进 prompt 的文本。系统会发现候选、确定性路由、安全加载选中的文件及其声明的辅助文件、写入审计事件，再把内容放进稳定 prompt prefix 的输入。

## 带 Skill 的一轮任务

```text
用户消息 + AGENTS.md
  -> discover_all_skills（项目根与可选全局根）
  -> SkillRouter 选 explicit/AGENTS/metadata 命中
  -> SkillLoader 校验 root-relative path 并加载
  -> 写 skill_selected / skill_loaded / required-file event
  -> system-prefix 构建接收 loaded skill context
  -> provider 在本轮看到这些指令
```

路由不调用模型，因此可复现，也不会为“选本地 instruction 文件”再多花一次模型调用。

## 发现：Skill 从哪里来

| 优先级 | 位置 | 来源 |
| ---: | --- | --- |
| 1 | `<project>/.agents/skills/*/SKILL.md` | 项目 agent skill |
| 2 | `<project>/skills/*.md` | 项目 markdown skill |
| 3 | `~/.agents/skills`、`~/.codex/skills`、`~/.firstcoder/skills` | 全局 agent/markdown skill |
| 4 | `FIRSTCODER_SKILL_ROOTS` 的逗号分隔目录 | 额外全局根 |

`<project>/skills/INDEX.md` 是目录说明，不是可执行 skill。设 `FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` 可仅发现项目 skill。frontmatter 可提供 `name`、`description`、逗号分隔 `triggers`；结果会排序去重，避免同一根重复出现导致目录不稳定。

## 核心数据与路由顺序

`SkillDefinition` 描述候选（name、path、source、root、description、triggers）；`SkillCatalog` 含候选、index 文本、fingerprint；`SkillRoutingDecision` 记录选择、候选、原因、字符串 confidence。

`SkillRouter` 严格按以下顺序：

1. 用户显式提到 name 或 path；
2. `AGENTS.md` 某行引用了 skill path，且这行规则与用户消息有有效重叠；
3. 用户消息与 name/description/triggers 的 token overlap。

metadata 命中歧义时会刻意不选。相同名称都命中则项目来源优先于全局来源。“不加载”比悄悄塞入无关指令安全得多。

## 加载被限制在 Root 内

`SkillLoader` 从登记 root 解析 skill path，越界即拒绝。它还会在“Required files”“Must read”及对应中文标题下提取 required file；这些文件再次要求位于同一个 root 内。

这只是路径包含规则，并不宣称 skill 内容天然可信；skill 能指导模型，但不能用 `../` 形式的 required file 去读任意文件。

## 审计、Resume 与变更语义

Session 会写入 `skill_selected`、`skill_loaded`、`skill_required_file_loaded`。loaded state 会保留在 runtime state，并在 resume 时回放。resume 重建“选过什么”，但会重新读仍存在的 skill 文件，所以不是旧内容的字节级快照。若要跨 skill 修改可复现，请把 skill 版本和项目一起固化，别假设 resume 自带时光机。

## 新增项目 Skill

1. 结构化流程优先放 `<project>/.agents/skills/<name>/SKILL.md`；简单流程可放 `<project>/skills/<name>.md`。
2. 写清 frontmatter description/triggers 或无歧义标题。
3. required 相对文件必须同 root，并列在 required-files 标题下。
4. 只有确实需要自动路由时才加 `AGENTS.md` route hint。
5. 测 discovery、explicit routing、歧义、path escape 拒绝。

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_skill_router.py \
  tests/test_skill_loader.py tests/test_agent_skill_flow.py -q
```

## 排障

| 现象 | 检查 |
| --- | --- |
| 找不到 skill | 根目录布局、disable-global flag、文件名、发现结果 |
| 选错 skill | 先看 explicit，再看 AGENTS 行重叠，最后看 metadata tie |
| 选中了但没进内容 | loader error/audit event 与 system-prefix input |
| required file 不该可读却读到了 | 应为 root-relative；path traversal 必须失败 |
| resume 行为不同 | 原 session 后 skill 文件已被修改 |

关联：[上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)、[代码阅读指南](CODEBASE_READING_GUIDE.zh-CN.md)。
