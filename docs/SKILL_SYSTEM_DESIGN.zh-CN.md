# 技能系统设计

[English Version](SKILL_SYSTEM_DESIGN.md)

## 概述

技能系统是一个用于复用工作流指令的确定性加载层。skill 并不是简单的提示词片段，而是：

- 可发现的文件
- 经过显式规则路由
- 在需要时加载 required files
- 在加载后写入 session events

这让 FirstCoder 能扩展工作流能力，但不会把 skill 变成任意可执行插件系统。

## 关键文件

- `firstcoder/skills/models.py`：skill catalog 和 definition 模型
- `firstcoder/skills/discovery.py`：基于文件系统的发现逻辑
- `firstcoder/skills/router.py`：确定性路由
- `firstcoder/skills/loader.py`：内容加载和 required-file 提取
- `firstcoder/skills/session.py`：skill 审计事件写入辅助逻辑
- `firstcoder/agent/loop.py`：每轮对话中的路由和加载集成
- `firstcoder/agent/session.py`：运行时状态中的 loaded skills

## Skill 来源

当前 discovery 同时支持项目内和机器级 skill roots。

项目内来源：

- `<project>/skills/*.md`
- `<project>/.agents/skills/*/SKILL.md`

全局 roots 包括：

- `~/.agents/skills`
- `~/.codex/skills`
- `~/.firstcoder/skills`
- `FIRSTCODER_SKILL_ROOTS` 提供的额外路径

通过 `FIRSTCODER_DISABLE_GLOBAL_SKILLS` 可以关闭全局 skills 发现。

## 核心模型

当前 `SkillDefinition` 包括：

- `name`
- `path`
- `source`
- `root`
- `description`
- `triggers`

`SkillCatalog` 包括：

- 发现到的 skills
- 可选的项目 `INDEX.md` 内容
- 一个计算出来的 fingerprint

当前实现不会在 `SkillDefinition` 上保存数值型 confidence。confidence 是在后续路由阶段生成的。

## 发现模型

discovery 完全由文件系统驱动。

`firstcoder/skills/discovery.py` 中的重要行为包括：

- 项目 `skills/INDEX.md` 会被读成 catalog index content，但它本身不是 skill
- markdown skill 通过 `.md` 文件发现
- agent skill 通过嵌套 `*/SKILL.md` 发现
- frontmatter 可以提供 `name`、`description` 和 `triggers`
- 结果会被稳定排序和去重

这样可以保证 catalog 在多次运行之间保持稳定，并避免不同 root 重叠导致的重复项。

## 路由模型

当前 skill 路由是确定性的，不依赖额外模型调用。

router 按顺序检查：

1. 用户消息里是否显式提到 skill 名称或路径
2. 项目说明（如 `AGENTS.md`）里的 route hint 是否命中
3. name、description、triggers 的 token overlap

最后会得到一个 routing decision，其中包括：

- 选中的 skill 或 `None`
- candidate 列表
- 路由原因
- 字符串形式的 confidence，例如 `high`、`medium`、`none`

当多个 skill 都能匹配时，运行时会优先项目内 skill。

## 加载模型

当当前回合选中了某个 skill，`AgentLoop` 会在构造 provider request 之前先把它加载进来。

加载行为包括：

1. 读取 skill 文件
2. 从 markdown 正文中提取 required-file 引用
3. 只在这些文件仍位于 skill root 内时加载它们
4. 把 skill 审计事件写入 session
5. 在构造 system prefix 时把加载内容注入进去

因此，skill 是 prompt 构造路径的一部分，而不是回答生成后的附加层。

## 审计与 Session 行为

当前 skill 相关审计事件包括：

- `skill_selected`
- `skill_loaded`
- `skill_required_file_loaded`

loaded skills 会保存在运行时状态中，并在 session resume 时重新恢复。

这里有一个重要行为：

- 先根据已有的 skill 事件重建已加载 skill 状态
- 再从当前磁盘重新读取技能文件（如果文件仍存在）

所以 loaded-skill 状态并不是“历史文件内容的完全快照”，而是部分依赖当前文件系统重建的。

## 优先级规则

当前有效优先级顺序是：

1. 项目 agent skill
2. 项目 markdown skill
3. 全局 agent skill
4. 全局 markdown skill

这样项目内工作流就能覆盖机器级默认规则，而不需要把路由器本身写得很复杂。

## 设计说明

- skills 的发现和路由是确定性的，不再额外调用模型。
- required-file 加载由内容驱动，并且严格限制在 skill root 内。
- skills 会在 session log 中留下可审计痕迹，方便事后理解 prompt 是怎么构造的。
- resume 更偏向“可重建的工作流状态”，而不是依赖黑盒技能缓存。
