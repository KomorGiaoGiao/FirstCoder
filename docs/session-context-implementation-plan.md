# Session 与 Context 完整实施计划

本文档是 `docs/session-context-plan.md` 的落地实施计划。目标不是先做一个最小 MVP，而是完整覆盖设计计划中提到的 session catalog、metadata、resume、只读 share、脱敏、TUI 接入和测试验收。

## 总目标

FirstCoder 需要把 `context` 已经承担的底层会话事实能力，和未来用户可见的 session 能力分清楚：

```text
firstcoder/context
  append-only event log
  SessionView rebuild
  runtime replay
  checkpoint
  archive
  context projection
  compaction pipeline

firstcoder/session
  session catalog
  session metadata
  resume service
  readonly transcript
  share export
  redaction

firstcoder/agent
  selected AgentSession runtime
  AgentLoop

firstcoder/app
  TUI command/input/output
```

完整实施完成后，用户应该能够：

- 查看已有会话列表。
- 查看单个会话摘要。
- resume 一个历史会话继续运行。
- 对当前或指定 session 生成只读 Markdown transcript。
- 默认安全地分享 transcript，不展开 archive 原文，不导出可恢复 snapshot。
- 在 TUI 中通过命令使用以上能力。

## 非目标

本计划不做以下事情：

- 不把 share 做成可导入、可继续运行的 session snapshot。
- 不上传公开 URL。
- 不迁移 SQLite。
- 不实现长期记忆或跨会话偏好。
- 不做破坏性 session 删除。
- 不默认读取 `.firstcoder/archives` 中的原始大工具输出。

## 实施原则

- 继续使用 JSONL 作为 session source of truth。
- catalog、transcript、share 都从 event log 派生，不另建消息存储。
- checkpoint 只影响 provider context 投影，不作为 resume 存储边界。
- share 默认偏保守：脱敏路径和 secret，不展开 tool result 原文。
- TUI widget 不直接扫描 JSONL，不直接拼 transcript。
- 每阶段先补测试，再实现代码。

## 当前实施状态

截至本轮实现，本文档中的阶段 1-11 已完成第一版落地：

- `firstcoder/session` 已包含 catalog、metadata、resume、redaction、transcript、share 和 session 层错误模型。
- `firstcoder/context` 继续持有 append-only event log、rebuild、runtime replay、checkpoint、archive 和 compaction。
- `firstcoder/app` 已接入 `/sessions`、`/session`、`/resume`、`/share`、`/rename`、`/context`、`/compact status`、`/compact`。
- 普通 TUI 输入已通过 `AgentChatRunner` 进入 `AgentLoop`，并展示 assistant 文本、tool call 和 tool result 摘要。
- `/resume` 会替换当前运行 session，`/context` 和普通聊天都会使用新 session。
- share 默认生成只读 Markdown transcript，不展开 archive 原文，不生成可 resume snapshot。
- `README.md`、`firstcoder/context/ARCHITECTURE.md` 和本实施计划已同步当前边界。

后续如果继续扩展，重点不再是“补齐本计划”，而是新增能力：例如 session 索引、HTML share、删除/归档 session、权限确认 UI 或真实 tokenizer。

## 阶段 1：补齐 session 包骨架

新增目录：

```text
firstcoder/session
├── __init__.py
├── errors.py
├── models.py
├── catalog.py
├── metadata.py
├── resume.py
├── redaction.py
├── transcript.py
└── share.py
```

职责说明：

- `models.py`：定义 session 层数据结构。
- `catalog.py`：扫描和汇总 session 列表。
- `metadata.py`：处理 metadata event 的合并和标题策略。
- `resume.py`：封装 resume 编排。
- `redaction.py`：纯文本脱敏。
- `transcript.py`：从 event log 派生只读 transcript。
- `share.py`：把 transcript 导出为 Markdown。
- `errors.py`：定义 session 层异常。

验收标准：

- `firstcoder/session` 不依赖 Textual。
- `firstcoder/session` 不执行工具、不调用 provider、不触发 compact。
- 基础 import 测试通过。

## 阶段 2：Session Catalog

实现 `SessionRecord`：

```python
@dataclass(slots=True)
class SessionRecord:
    session_id: str
    title: str
    created_at: str | None
    updated_at: str | None
    workspace: str | None
    provider: str | None
    model: str | None
    message_count: int
    user_turn_count: int
    checkpoint_count: int
    archive_count: int
    latest_user_input: str | None
    latest_assistant_output: str | None
    latest_checkpoint_id: str | None
    status: str
    error: str | None = None
```

实现 `SessionCatalog`：

```python
class SessionCatalog:
    def list_sessions(self) -> list[SessionRecord]: ...
    def get_session(self, session_id: str) -> SessionRecord: ...
    def exists(self, session_id: str) -> bool: ...
```

扫描规则：

- 扫描 `.firstcoder/sessions/*.jsonl`。
- 按 `updated_at` 倒序返回。
- 单个 JSONL 损坏时生成 `status="corrupt"`，不影响其他 session。
- 空文件或无有效事件生成 `status="empty"`。
- 正常 session 生成 `status="ok"`。

字段推断规则：

- `created_at` 来自第一条有效事件。
- `updated_at` 来自最后一条有效事件。
- `title` 优先 metadata title，其次第一条用户消息预览，其次 session id。
- `workspace` 优先 metadata workspace。
- `provider/model` 优先最近 assistant message metadata。
- `checkpoint_count` 来自 `checkpoint_created` 事件数量。
- `archive_count` 从 message part metadata 中的 `archive_id` 或 archive placeholder 统计。

测试：

```text
tests/test_session_catalog.py
```

覆盖：

- 空目录返回空列表。
- 多 session 按更新时间倒序。
- 第一条用户消息生成默认标题。
- provider/model 从 assistant metadata 推断。
- checkpoint/archive 数量正确。
- 损坏 JSONL 不阻断列表。
- `get_session()` 找不到时抛出 session 层错误。

## 阶段 3：Session Metadata Event

新增事件类型：

```text
session_metadata_updated
```

payload 使用 patch 语义：

```json
{
  "title": "实现 resume 列表",
  "workspace": "D:\\Komor_Code\\FirstCoder",
  "updated_at": "2026-06-03T12:00:00Z"
}
```

修改点：

- `SessionEventWriter` 增加 `append_session_metadata_updated()`。
- `JsonlSessionStore.rebuild_session_view()` 合并 metadata patch。
- `SessionCatalog` 重放 metadata patch。
- `AgentSession.create()` 可写入初始 workspace/title，但避免高频 metadata 更新。

约束：

- `updated_at` 优先由 catalog 根据最后事件推断，不每轮写 metadata。
- provider/model 优先从 assistant message metadata 推断。
- 第一版只在创建、手动 rename、workspace 显式变化时写 metadata event。

测试：

```text
tests/test_session_metadata.py
```

覆盖：

- metadata patch 按事件顺序合并。
- title 更新影响 catalog。
- metadata event 不生成普通 provider message。
- rebuild session view 后 metadata 可见。

## 阶段 4：Resume Service

实现：

```python
@dataclass(slots=True)
class ResumeResult:
    session: AgentSession
    record: SessionRecord

class ResumeService:
    def resume(self, session_id: str) -> ResumeResult: ...
```

职责：

- 校验 session 是否存在且不是 corrupt。
- 读取当前项目 `AGENTS.md`。
- 调用 `AgentSession.resume()`。
- 返回 runtime session 和 catalog record。
- 明确不从 checkpoint 后恢复，而是读取完整 event log。

测试：

```text
tests/test_session_resume_service.py
```

覆盖：

- 正常 session 可 resume。
- resume 后 turn counter 正确。
- resume 后 known message ids 正确。
- resume 后 runtime state 可重放 task hash 和 compact 状态。
- corrupt session 拒绝 resume。
- 不存在 session 抛出明确错误。

## 阶段 5：Redaction

实现：

```python
@dataclass(slots=True)
class RedactionOptions:
    redact_paths: bool = True
    redact_secrets: bool = True

def redact_text(text: str, options: RedactionOptions) -> str: ...
```

脱敏规则：

- 包含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`COOKIE` 的键值片段脱敏。
- Windows 绝对路径可脱敏为 `[REDACTED_PATH]`。
- POSIX 绝对路径可脱敏为 `[REDACTED_PATH]`。
- 不试图做完美安全扫描，但默认行为必须保守。

测试：

```text
tests/test_session_redaction.py
```

覆盖：

- API key/token/password/cookie 样式文本脱敏。
- Windows 路径脱敏。
- POSIX 路径脱敏。
- 关闭 path redaction 时保留路径。
- 关闭 secret redaction 时保留 secret 样式文本。

## 阶段 6：Readonly Transcript

实现 transcript 中间结构：

```python
@dataclass(slots=True)
class TranscriptEntry:
    role: str
    title: str
    content: str
    message_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

@dataclass(slots=True)
class Transcript:
    session: SessionRecord
    entries: list[TranscriptEntry]
```

实现：

```python
class TranscriptBuilder:
    def build(self, session_id: str, options: ShareOptions) -> Transcript: ...
```

默认展示策略：

- user text：保留并脱敏。
- assistant text：保留并脱敏。
- assistant tool call：显示工具名和参数摘要。
- tool result：默认不显示全文，只显示状态和摘要。
- checkpoint summary：显示为“历史压缩摘要”。
- compaction event：默认不展示，开启 metadata 选项时展示摘要。
- archive placeholder：显示 archive summary，不读取 archive 原文。
- system prompt：默认不导出。

测试：

```text
tests/test_session_transcript.py
```

覆盖：

- conversation 顺序稳定。
- message id 可保留。
- tool call 只显示摘要。
- tool result 默认不展开。
- archive 默认不读取原文。
- checkpoint summary 可读。
- compaction metadata 可按选项显示。
- transcript 不是可 resume snapshot。

## 阶段 7：Share Markdown Export

实现：

```python
@dataclass(slots=True)
class ShareOptions:
    include_event_ids: bool = False
    include_compaction_metadata: bool = False
    include_tool_calls: bool = True
    include_tool_results: bool = False
    max_tool_result_chars: int = 1200
    redact_paths: bool = True
    redact_secrets: bool = True
    archive_mode: str = "placeholder"

class SessionShareService:
    def export_markdown(self, session_id: str, output_path: Path | None = None, options: ShareOptions | None = None) -> Path: ...
```

默认输出：

```text
.firstcoder/shares/{session_id}.md
```

Markdown 结构：

```markdown
# <session title>

- Session: <session_id>
- Created: <created_at>
- Updated: <updated_at>
- Workspace: <redacted or value>
- Model: <provider>/<model>

## Conversation

### User

...

### Assistant

...

### Tool: shell

- Status: success
- Summary: output omitted for sharing
```

安全约束：

- 默认不读取 archive 原文。
- 默认不展开 tool result 全文。
- 默认脱敏路径和 secret。
- 导出文件不是 JSONL，不可用于 resume。

测试：

```text
tests/test_session_share.py
```

覆盖：

- 默认路径正确。
- Markdown 内容包含 session header。
- 默认不包含 archive 原文。
- 默认不包含完整 tool result。
- 开启 `include_tool_results` 时仍受 `max_tool_result_chars` 限制。
- 重复导出可覆盖同一路径。

## 阶段 8：Session TUI 命令

新增 `SessionCommandHandler`，避免把 session 命令全部塞进 `ContextCommandHandler`。

建议命令：

```text
/sessions
/session <session_id>
/resume <session_id>
/share
/share <session_id>
/share <session_id> --tool-results
/rename <title>
```

命令行为：

- `/sessions`：展示 catalog 列表。
- `/session <session_id>`：展示单个 session 摘要。
- `/resume <session_id>`：调用 `ResumeService`，切换当前运行 session。
- `/share`：分享当前 session。
- `/share <session_id>`：分享指定 session。
- `/rename <title>`：写入 `session_metadata_updated`。

TUI 边界：

- Textual widget 只调用 command handler。
- command handler 只调用 session/context/agent 服务。
- 不在 widget 中扫描 `.firstcoder/sessions`。

测试：

```text
tests/test_app_session_commands.py
```

覆盖：

- `/sessions` 输出列表。
- `/session <id>` 输出摘要。
- `/resume <id>` 调用 resume service。
- `/share` 输出本地 markdown 路径。
- `/rename` 写 metadata event。
- 未知 session 输出明确错误。

## 阶段 9：TUI 普通聊天与当前 Session 状态

当前 `FirstCoderApp` 普通聊天入口尚未接入 `AgentLoop`。本阶段完成 session 计划的用户闭环。

实现目标：

- TUI 启动时创建或恢复当前 `AgentSession`。
- 普通输入调用 `AgentLoop.run_user_turn()`。
- 输出 assistant 文本。
- 简要展示 tool call 和 tool result。
- `/resume` 后替换当前 session。
- Header 或状态区显示当前 session title/session_id。
- `/context`、`/compact status`、`/compact` 继续使用当前 session。

约束：

- provider 配置仍走现有 config/provider factory。
- 不在本阶段美化复杂 UI。
- 不让 agent 编排逻辑写进 Textual widget。

测试：

```text
tests/test_app_tui.py
tests/test_app_runtime.py
tests/test_app_factory.py
tests/test_app_session_commands.py
```

覆盖：

- 普通输入进入 AgentLoop。
- `/resume` 后当前 session 改变。
- `/context` 使用 resume 后的当前 session。
- tool call 摘要可显示。

## 阶段 10：文档同步

更新：

```text
README.md
firstcoder/context/ARCHITECTURE.md
docs/session-context-plan.md
docs/session-context-implementation-plan.md
```

同步内容：

- README 中 `docs/session-context-plan.md` 链接必须存在且准确。
- `context/ARCHITECTURE.md` 明确 `context` 不负责 catalog/share。
- `session-context-plan.md` 保持设计说明。
- 本文档记录实施状态。

## 阶段 11：全量测试与验收

新增或更新测试文件：

```text
tests/test_session_catalog.py
tests/test_session_metadata.py
tests/test_session_resume_service.py
tests/test_session_redaction.py
tests/test_session_transcript.py
tests/test_session_share.py
tests/test_app_session_commands.py
tests/test_app_tui.py
tests/test_app_runtime.py
tests/test_app_factory.py
```

最终验收：

- `pytest` 全量通过。
- session catalog 能列出多个历史会话。
- corrupt session 不影响其他 session。
- resume 使用完整 event log。
- checkpoint 不被误认为 resume 边界。
- share 默认只读、安全、不可 resume。
- archive 原文默认不展开。
- TUI 命令覆盖 sessions/session/resume/share/rename。
- 文档和代码边界一致。

## 推荐开发顺序

按以下顺序实施，避免 UI 先行导致底层边界混乱：

```text
1. firstcoder/session models + errors
2. SessionCatalog
3. metadata event
4. ResumeService
5. redaction
6. TranscriptBuilder
7. SessionShareService
8. SessionCommandHandler
9. TUI 当前 session 状态和普通聊天入口
10. 文档同步和全量测试
```

每一步完成后更新本文档状态，避免计划和实际实现继续漂移。
