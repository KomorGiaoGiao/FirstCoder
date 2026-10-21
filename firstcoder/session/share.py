"""只读 session share 导出。

第一版 share 只导出本地 Markdown transcript，不生成可导入、可继续运行的
session snapshot。
"""

from __future__ import annotations

from firstcoder.utils.text import display_value, model_label

from dataclasses import dataclass, replace
from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.session.models import PiiOptions, RedactionOptions, ShareOptions, Transcript, TranscriptEntry
from firstcoder.session.redaction import redact_text
from firstcoder.session.transcript import TranscriptBuilder


@dataclass(slots=True)
class SessionShareService:
    store: JsonlSessionStore
    transcript_builder: TranscriptBuilder | None = None
    # 从应用配置注入的 PII 策略; 调用方未显式指定 ShareOptions.pii 时生效
    pii_options: PiiOptions | None = None

    def export_markdown(
        self,
        session_id: str,
        *,
        output_path: str | Path | None = None,
        options: ShareOptions | None = None,
    ) -> Path:
        """导出流程: 事件日志 -> 只读 Transcript -> 渲染 Markdown -> 写盘, 全程不改源会话。"""

        resolved = options or ShareOptions()
        if resolved.pii is None and self.pii_options is not None:
            resolved = replace(resolved, pii=self.pii_options)
        builder = self.transcript_builder or TranscriptBuilder(self.store)
        transcript = builder.build(session_id, resolved)
        path = Path(output_path) if output_path is not None else self.store.root / "shares" / f"{session_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(transcript, options=resolved), encoding="utf-8")
        return path


def _render_markdown(transcript: Transcript, *, options: ShareOptions) -> str:
    # ShareOptions 默认保守: 不展开工具结果, archive 只给占位/预览; 标题, workspace 等
    # 元数据同样要过 redact_text, 因为标题往往就是用户输入, 可能含路径或密钥
    session = transcript.session
    redaction = RedactionOptions(redact_paths=options.redact_paths, redact_secrets=options.redact_secrets, pii=options.pii)
    title = redact_text(session.title, redaction)
    lines = [
        f"# {_clean_heading(title)}",
        "",
        f"- Session: {session.session_id}",
        f"- Created: {display_value(session.created_at)}",
        f"- Updated: {display_value(session.updated_at)}",
        f"- Workspace: {display_value(redact_text(session.workspace or '', redaction) if session.workspace else None)}",
        f"- Model: {model_label(session.provider, session.model)}",
        "",
        "## Conversation",
        "",
    ]
    if not transcript.entries:
        lines.extend(["_No conversation entries._", ""])
    for entry in transcript.entries:
        lines.extend(_render_entry(entry, include_event_ids=options.include_event_ids))
    return "\n".join(lines).rstrip() + "\n"


def _render_entry(entry: TranscriptEntry, *, include_event_ids: bool) -> list[str]:
    lines = [f"### {_clean_heading(entry.title)}", ""]
    if entry.message_id:
        lines.append(f"- Message: {entry.message_id}")
    if include_event_ids and entry.metadata.get("event_id"):
        lines.append(f"- Event: {entry.metadata['event_id']}")
    if len(lines) > 2:
        lines.append("")
    lines.extend([_fenced(entry.content), ""])
    return lines


def _fenced(content: str) -> str:
    """围栏长度随内容自适应: 内容里出现 ``` 时加长围栏, 保证代码块不会被内容顶穿。"""

    fence = "```"
    while fence in content:
        fence += "`"
    return "\n".join([fence, content, fence])


def _clean_heading(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized or "Untitled"
