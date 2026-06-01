# FirstCoder 上下文正式开发实施计划

本文档把 `context-compact-plan.md` 拆成可逐步实施的开发切片。原则是 TDD、小步合并、每一阶段都有可运行测试；不要一次性实现完整自动压缩系统。

当前已完成的架构准备：

```text
firstcoder/context/models.py
firstcoder/context/events.py
firstcoder/context/store.py
firstcoder/context/context_builder.py
firstcoder/context/tool_result.py
firstcoder/context/token_budget.py
firstcoder/context/runtime_state.py
firstcoder/context/identity.py
firstcoder/context/versions.py
```

## 实施边界

本计划处理的是上下文窗口管理，不是长期记忆。

```text
放在 firstcoder/context：
  system prompt 稳定前缀
  prompt prefix cache
  tool result archive
  checkpoint
  resume projection
  task hash 边界辅助
  L1-L4 compaction pipeline

暂不放在 firstcoder/memory：
  长期用户偏好
  跨会话知识库
  语义检索记忆
```

## 开发节奏

每一轮开发都按这个顺序：

```text
1. 先写失败测试。
2. 实现最小代码让测试通过。
3. 跑相关测试。
4. 跑全量测试。
5. 更新计划状态和必要文档。
```

如果某一轮需要改 provider 或 tool 层，只做上下文功能需要的最小适配，不顺手重构无关模块。

## 第一阶段：SystemPromptBuilder 与 PromptPrefixCache

目标：

```text
系统提示词不进入普通会话历史。
稳定输入不变时复用 stable prefix。
AGENTS.md、工具 schema、provider 能力、版本变化时 fingerprint 改变。
普通 user/assistant/tool 消息追加时 fingerprint 不变。
```

建议新增模块：

```text
firstcoder/context/system_prompt.py
```

建议新增测试：

```text
tests/test_context_system_prompt.py
```

测试用例：

```text
test_system_prompt_fingerprint_is_stable_for_same_inputs
test_system_prompt_cache_reuses_prefix_when_fingerprint_matches
test_agents_md_change_invalidates_system_prompt_fingerprint
test_tool_schema_change_invalidates_system_prompt_fingerprint
test_conversation_messages_do_not_invalidate_system_prompt_fingerprint
```

验收标准：

```text
ContextBuilder 可以接收 stable system prefix。
SessionStore 不保存 system prompt 原文为普通消息。
fingerprint 输入只包含稳定配置，不包含动态 token 统计和最近消息。
```

停止点：

```text
不接入真实 agent loop。
不实现自动压缩。
```

## 第二阶段：Archive 大工具结果落盘

目标：

```text
工具输出过大时完整内容落盘。
上下文里只保留 placeholder、summary、preview、archive_id。
resume 时默认不展开 archive 原文。
重复处理同一个 archive_id 时不重复落盘。
```

建议新增模块：

```text
firstcoder/context/archive.py
```

建议新增测试：

```text
tests/test_context_archive.py
```

测试用例：

```text
test_large_tool_result_is_written_to_archive
test_archive_placeholder_keeps_archive_id_summary_and_preview
test_archived_tool_result_is_not_archived_twice
test_resume_projection_keeps_archive_placeholder
```

验收标准：

```text
.firstcoder/archives/<session_id>/<archive_id>.txt 保存原文。
.firstcoder/archives/<session_id>/<archive_id>.json 保存 metadata。
MessagePart.metadata 写入 archive_id、original_tokens、preview_tokens、compaction_state=archived。
```

停止点：

```text
不做 L1/L2 pipeline 编排。
只提供 archive 能力和可测试 API。
```

## 第三阶段：Checkpoint 与简单 Resume 投影

目标：

```text
latest checkpoint 代表已经折叠的旧历史。
ContextBuilder 投影 latest checkpoint summary + recent tail。
tail_start_message_id 之后的消息保留原文。
checkpoint 覆盖过的旧历史不重复投影。
```

建议新增模块：

```text
firstcoder/context/checkpoint.py
```

建议新增测试：

```text
tests/test_context_checkpoint.py
tests/test_context_resume.py
```

测试用例：

```text
test_latest_checkpoint_is_selected
test_context_projection_uses_checkpoint_summary_and_tail
test_messages_before_tail_are_not_projected_twice
test_tail_start_message_id_moves_monotonically
test_resume_does_not_expand_archived_tool_result
```

验收标准：

```text
Checkpoint 数据结构包含 tail_start_message_id、covered_until_message_id、source_fingerprint、strategy_version。
ContextBuilder 明确区分 checkpoint summary 和普通消息 tail。
不会因为 task hash 变化自动移动 tail_start_message_id。
```

停止点：

```text
不做 LLM summary。
不做中间 snip。
checkpoint summary 先由测试或调用方传入。
```

## 第四阶段：Task Hash 工具与稳定窗口

目标：

```text
模型只提交 same/new/uncertain。
hash 由程序生成，格式稳定。
new 候选稳定后才确认切换。
确认切换后只触发 compaction pipeline 请求，不直接写 checkpoint。
```

建议新增模块：

```text
firstcoder/context/task_boundary.py
```

后续接入工具层时再新增：

```text
firstcoder/tools/task_boundary.py
```

建议新增测试：

```text
tests/test_context_task_boundary.py
```

测试用例：

```text
test_same_keeps_active_task_hash
test_uncertain_keeps_active_task_hash
test_new_requires_stable_window
test_confirmed_change_returns_compaction_trigger
test_task_hash_event_records_candidate_and_confirmation
```

验收标准：

```text
工具参数保持极简：decision、basis_message_id。
SessionRuntimeState 维护 candidate_hash 和 stable_count。
TaskHashEvent 可重放调试。
```

停止点：

```text
不让模型自由输出 hash。
不实现真实自动压缩执行，只返回 trigger 信号。
```

## 第五阶段：L1-L3 程序化压缩

目标：

```text
自动压缩优先不用 LLM。
L1 只处理旧任务或跨任务内容。
L2 只处理大结果 archive。
L3 处理本次任务内相对冷的信息。
每层后重新估算 token，达标就停止。
```

建议新增模块：

```text
firstcoder/context/compaction.py
firstcoder/context/content/detector.py
firstcoder/context/content/router.py
firstcoder/context/content/compressors.py
```

建议新增测试：

```text
tests/test_context_compaction_pipeline.py
tests/test_context_content_detector.py
tests/test_context_content_compressors.py
```

测试用例：

```text
test_l1_skips_current_task_content
test_l2_skips_already_archived_part
test_l3_only_handles_current_task_cold_content
test_pipeline_stops_after_budget_target_is_met
test_noop_compaction_is_recorded_and_deduped
```

验收标准：

```text
part metadata 的 compaction_state 防止重复压缩。
CompactionEvent 记录 before_tokens、after_tokens、levels_attempted、stopped_at。
相同 input_fingerprint 的 no-op 不反复执行。
```

停止点：

```text
不实现 LLM compact。
内容 compressor 先做轻量确定性版本。
```

## 第六阶段：L4 LLM Compact、重试、兜底、熔断

目标：

```text
前三层不能达标时才调用 LLM。
LLM compact 成功后写 checkpoint。
prompt_too_long、timeout、no_summary 有有限重试。
自动 compact 连续失败后熔断。
```

建议新增模块：

```text
firstcoder/context/llm_compact.py
firstcoder/context/retry_policy.py
```

建议新增测试：

```text
tests/test_context_llm_compact.py
tests/test_context_retry_policy.py
tests/test_context_circuit_breaker.py
```

测试用例：

```text
test_l4_writes_checkpoint_on_success
test_prompt_too_long_retries_after_stronger_compaction
test_timeout_uses_limited_backoff_retries
test_auto_compact_failure_opens_circuit_breaker_after_limit
test_manual_compact_ignores_auto_circuit_breaker
```

验收标准：

```text
LLM summary prompt 明确禁止总结 system prompt 和 tool schema。
checkpoint source_fingerprint 防止同一批历史重复总结。
SessionRuntimeState 记录 failure_count、disabled_until、last_failure_reason。
```

## 第七阶段：调试视图与集成入口

目标：

```text
能查看当前上下文状态。
能手动触发 compact。
能解释为什么自动压缩跳过、成功或失败。
```

建议新增模块：

```text
firstcoder/context/inspector.py
```

建议新增测试：

```text
tests/test_context_inspector.py
```

报告字段：

```text
session_id
active_task_hash
candidate_task_hash
system_prompt_fingerprint
latest_checkpoint_id
tail_message_count
estimated_tokens
archive_count
last_compaction_input_fingerprint
auto_compact_disabled_until
last_failure_reason
```

验收标准：

```text
调试输出来自结构化状态，不从自然语言上下文里解析。
可以用于 TUI 的 /context 或 /compact status。
```

## 当前下一步

下一次正式编码建议从第一阶段开始：

```text
1. 新增 tests/test_context_system_prompt.py。
2. 实现 firstcoder/context/system_prompt.py。
3. 让 ContextBuilder 接收 stable system prefix。
4. 跑 test_context_system_prompt.py。
5. 跑 python -m pytest。
```

