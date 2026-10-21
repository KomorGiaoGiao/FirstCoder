"""基于 JSONL 的会话事件存储。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.events import SessionEvent
from firstcoder.context.metadata import merge_metadata_patch
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.planning.models import TaskPlan, TaskPlanError
from firstcoder.planning.validation import validate_plan

# 事件类型 -> 重放角色映射: 类型决定 replay 时折叠成哪种 AgentMessage.
# background_notification 以 user 角色进入历史, 让模型把后台通知当作用户侧的新信息.
EVENT_ROLE_MAP = {
    "user_message": "user",
    "assistant_message": "assistant",
    "tool_result": "tool",
    "background_notification": "user",
}


class SessionStoreCorruptError(ValueError):
    """持久化事件无法重放成可信的会话视图时抛出 (事实文件损坏, 宁可报错也不带着脏状态继续)."""


class JsonlSessionStore:
    """append-only JSONL store。

    JSONL 事件文件是事实层: 只增不改, resume 靠从头到尾重放事件还原视图; 压缩也
    只是追加新事件, 从不回头修改历史事件. 当前阶段选择 JSONL 是为了让 resume,
    压缩事件和调试记录都能被人工阅读. 后续迁移 SQLite 时, 外部仍应保留
    `append_event/list_events/rebuild_session_view` 这组边界.
    """

    def __init__(self, root: str | Path) -> None:
        # 磁盘布局: root/sessions/<session_id>.jsonl 存事件日志, root/archives/<session_id>/
        # 存压缩原文归档, root/session_index.json 是可重建的列表缓存.
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: SessionEvent) -> None:
        # 事实层写入只有一种姿势: 追加一行 JSON, 绝不改写已有行
        path = self._session_path(event.session_id)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
            file.write("\n")
        from firstcoder.session.index import SessionIndex

        # 顺带增量更新列表索引; 索引只是缓存, 就算失败事实也已落盘
        SessionIndex(self.root).update_event(event)

    def list_events(self, session_id: str) -> list[SessionEvent]:
        path = self._session_path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    # 读取的每一行都是一个SessionEvent
                    events.append(SessionEvent.from_dict(json.loads(line)))
        return events

    def rebuild_session_view(self, session_id: str) -> SessionView:
        """重放会话事件日志, 还原出当前内存视图(空会话返回空视图).

        重放是 resume 的唯一入口: 视图不单独落盘, 永远从 append-only 的事实事件重建.
        """

        view = SessionView(session_id=session_id)
        for sequence, event in enumerate(self.list_events(session_id), start=1):
            self._apply_event(view, event, sequence=sequence)
        return view

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def _apply_event(self, view: SessionView, event: SessionEvent, *, sequence: int) -> None:
        # 重放必须幂等: 同一事件无论重放多少次, 得到的视图都一致, 因此这里只做确定性套用
        # 追加到view.metadata
        if event.type in {"session_created", "session_metadata_updated"}:
            view.metadata = merge_metadata_patch(view.metadata, event.payload)
            view.metadata["session_id"] = event.session_id
            return

        # 追加到view.checkpoints
        if event.type == "checkpoint_created":
            view.checkpoints.append(Checkpoint.from_dict(_checkpoint_payload(event, sequence=sequence)))
            return

        # 替换view.messages
        if event.type == "compaction_completed":
            _apply_compaction_replacements(view, event)
            return

        # 更新已有part
        if event.type == "message_part_metadata_updated":
            _apply_message_part_metadata_update(view, event)
            return

        # 追加到 view.task_plan
        if event.type == "task_plan_updated":
            _apply_task_plan_payload(view, event)
            return

        # 4种消息类, 追加到 view.messages
        role = EVENT_ROLE_MAP.get(event.type)
        if role is None:
            return

        message = _message_from_event(event, role=role)
        view.messages.append(message)


def _message_from_event(event: SessionEvent, *, role: str) -> AgentMessage:
    payload = event.payload
    message_id = str(payload["message_id"])
    parts = _parts_from_payload(payload.get("parts", []), message_id=message_id)
    return AgentMessage(
        id=message_id,
        session_id=event.session_id,
        role=role,
        parts=parts,
        created_at=event.created_at,
        metadata=dict(payload.get("metadata") or {}),
    )


def _parts_from_payload(parts: Iterable[dict[str, object]], *, message_id: str) -> list[MessagePart]:
    result: list[MessagePart] = []
    for part in parts:
        data = dict(part)
        data.setdefault("message_id", message_id)
        result.append(MessagePart.from_dict(data))
    return result


def _checkpoint_payload(event: SessionEvent, *, sequence: int) -> dict[str, object]:
    payload: dict[str, object] = dict(event.payload)
    payload.setdefault("created_at", event.created_at)
    payload.setdefault("session_id", event.session_id)
    payload.setdefault("sequence", sequence)
    return payload


def _apply_compaction_replacements(view: SessionView, event: SessionEvent) -> None:
    # 压缩替换的重放也要求幂等: 按 (message_id, part_id) 定点替换, 重复套用同一事件结果一致
    event_payload = event.payload.get("event")
    if not isinstance(event_payload, dict):
        return

    replacements = event_payload.get("replacements")
    if not isinstance(replacements, list):
        return

    part_index: dict[tuple[str, str], tuple[AgentMessage, int]] = {}
    for message in view.messages:
        for index, part in enumerate(message.parts):
            part_index[(message.id, part.id)] = (message, index)

    for item in replacements:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or "")
        source_part_id = str(item.get("source_part_id") or "")
        replacement_part = item.get("replacement_part")
        if not message_id or not source_part_id or not isinstance(replacement_part, dict):
            continue
        target = part_index.get((message_id, source_part_id))
        if target is None:
            continue
        message, index = target
        replacement_data = dict(replacement_part)
        replacement_data.setdefault("message_id", message_id)
        message.parts[index] = MessagePart.from_dict(replacement_data)


def _apply_message_part_metadata_update(view: SessionView, event: SessionEvent) -> None:
    message_id = str(event.payload.get("message_id") or "")
    part_id = str(event.payload.get("part_id") or "")
    metadata = event.payload.get("metadata")
    if not message_id or not part_id or not isinstance(metadata, dict):
        return
    for message in view.messages:
        if message.id != message_id:
            continue
        for part in message.parts:
            if part.id == part_id:
                part.metadata.update(metadata)
                return


def _apply_task_plan_payload(view: SessionView, event: SessionEvent) -> None:
    try:
        plan = TaskPlan.from_dict(event.payload.get("snapshot"))  # type: ignore[arg-type]
        validate_plan(plan)
    except (TaskPlanError, TypeError) as error:
        raise SessionStoreCorruptError(f"invalid task_plan_updated snapshot in event {event.id}: {error}") from error

    # revision 链必须连续: previous_revision 等于当前 revision, 且每条只前进 1;
    # 断链说明事件丢失或文件被改过, 重放出的计划不可信, 直接判损坏
    previous_revision = event.payload.get("previous_revision")
    revision = event.payload.get("revision")
    if isinstance(previous_revision, bool) or not isinstance(previous_revision, int) or isinstance(revision, bool) or not isinstance(revision, int):
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event.id}")
    expected_previous = view.task_plan.revision if view.task_plan is not None else 0
    if previous_revision != expected_previous or revision != previous_revision + 1:
        raise SessionStoreCorruptError(f"task_plan_updated revision chain is invalid in event {event.id}: " f"expected previous {expected_previous}, got {previous_revision} -> {revision}")
    if revision != plan.revision:
        raise SessionStoreCorruptError(f"task_plan_updated revision mismatch in event {event.id}")
    view.task_plan = plan
