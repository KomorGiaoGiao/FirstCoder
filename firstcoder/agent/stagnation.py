"""Benchmark 工具停滞检测。

停滞状态只存在于当前用户回合，不写入长期会话事实。这样普通 TUI 不会被 benchmark
策略影响，新的任务边界也不会继承上一题的失败计数。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from firstcoder.agent.execution_evidence import has_resetting_success
from firstcoder.providers.types import ToolCall
from firstcoder.tools.types import ToolResult, make_error_result

_OUTPUT_TAIL_CHARS = 1200
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class StagnationGuard:
    """识别相同参数产生的相同失败，并在第四次执行前阻断。"""

    failure_counts: dict[str, int] = field(default_factory=dict)
    blocked_call_keys: set[str] = field(default_factory=set)
    background_signature: str | None = None
    background_poll_count: int = 0

    def reset(self) -> None:
        self.failure_counts.clear()
        self.blocked_call_keys.clear()
        self.background_signature = None
        self.background_poll_count = 0

    def validate(self, tool_call: ToolCall) -> ToolResult | None:
        call_key = _call_key(tool_call)
        if call_key not in self.blocked_call_keys:
            return None
        return make_error_result(
            tool_call.name,
            "已阻止第 4 次原样重复调用：相同工具参数已连续产生至少 3 次相同失败。"
            "请改变参数、检查前置条件，或采用不同的诊断/实现策略。",
            stagnation_blocked=True,
            repeated_call_key=call_key,
        )

    def observe(self, tool_call: ToolCall, result: ToolResult) -> str | None:
        if _is_ignored_result(result):
            return None
        if tool_call.name == "background_status":
            return self._observe_background_status(result)
        if has_resetting_success(tool_call, result):
            self.reset()
            return None
        if result.ok:
            return None

        fingerprint = _failure_fingerprint(tool_call, result)
        count = self.failure_counts.get(fingerprint, 0) + 1
        self.failure_counts[fingerprint] = count
        if count >= 3:
            self.blocked_call_keys.add(_call_key(tool_call))
        if count == 2:
            return (
                "Stagnation warning: this exact tool call has produced the same failure twice. "
                "Inspect the cause and switch strategy or parameters instead of retrying unchanged."
            )
        if count == 3:
            return (
                "Stagnation guard armed: this exact failure occurred three times. "
                "A fourth unchanged call will be blocked; use a different approach."
            )
        return None

    def _observe_background_status(self, result: ToolResult) -> str | None:
        snapshots = _background_snapshots(result)
        running = sorted(
            str(item.get("job_id") or "")
            for item in snapshots
            if item.get("status") == "running"
        )
        if not running:
            self.background_signature = None
            self.background_poll_count = 0
            return None
        signature = json.dumps(running, ensure_ascii=True, separators=(",", ":"))
        if signature == self.background_signature:
            self.background_poll_count += 1
        else:
            self.background_signature = signature
            self.background_poll_count = 1
        if self.background_poll_count == 3:
            return (
                "Background polling warning: the same jobs are still running after three status checks. "
                "Do useful independent work or allow a meaningful interval before polling again."
            )
        return None


def append_guidance(result: ToolResult, guidance: str | None) -> None:
    """把一次性策略提示附到即将持久化的工具结果中。"""

    if not guidance:
        return
    result.content = f"{result.content.rstrip()}\n\n[agent guidance]\n{guidance}"
    existing = result.data.get("agent_guidance")
    if isinstance(existing, list):
        existing.append(guidance)
    elif isinstance(existing, str) and existing:
        result.data["agent_guidance"] = [existing, guidance]
    else:
        result.data["agent_guidance"] = [guidance]


def _call_key(tool_call: ToolCall) -> str:
    payload = {
        "tool": tool_call.name,
        "arguments": _normalize_value(tool_call.arguments),
    }
    return _digest(payload)


def _failure_fingerprint(tool_call: ToolCall, result: ToolResult) -> str:
    payload = {
        "call_key": _call_key(tool_call),
        "ok": result.ok,
        "error": _normalize_text(result.error or ""),
        "exit_code": result.data.get("exit_code"),
        "output_tail": _normalize_text(result.content[-_OUTPUT_TAIL_CHARS:]),
    }
    return _digest(payload)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str):
        return _normalize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


def _normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _background_snapshots(result: ToolResult) -> list[dict[str, object]]:
    single = result.data.get("job")
    if isinstance(single, dict):
        return [single]
    multiple = result.data.get("jobs")
    if isinstance(multiple, list):
        return [item for item in multiple if isinstance(item, dict)]
    return []


def _is_ignored_result(result: ToolResult) -> bool:
    request_type = result.data.get("request_type")
    return bool(
        result.data.get("requires_user_input")
        or result.data.get("skipped_due_to_user_input")
        or result.data.get("interrupted")
        or result.data.get("stagnation_blocked")
        or (
            isinstance(request_type, str)
            and request_type.startswith(("permission_", "prewrite_review_"))
        )
    )
