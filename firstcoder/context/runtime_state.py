"""会话运行期状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _utc_after(minutes: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=minutes)).isoformat().replace(
        "+00:00",
        "Z",
    )


def parse_utc_iso(value: str) -> datetime:
    """解析 JSONL/runtime state 使用的 UTC ISO 字符串。

    会话状态里主要写入 `...Z` 形式；这里也兼容没有时区的值，并按 UTC 处理，避免不同
    调用点各自实现时产生熔断过期判断漂移。
    """

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def active_auto_compact_disabled_until(state: "SessionRuntimeState") -> str | None:
    """返回仍有效的自动压缩禁用截止时间，过期则返回 None。"""

    if not state.auto_compact_disabled_until:
        return None

    disabled_until = parse_utc_iso(state.auto_compact_disabled_until)
    if disabled_until > datetime.now(UTC):
        return state.auto_compact_disabled_until
    return None


def auto_compact_circuit_is_open(state: "SessionRuntimeState") -> bool:
    """判断自动压缩熔断是否仍打开。

    这里会顺手清理已经过期的 disabled_until，让后续 compact/status 看到一致状态。
    """

    if active_auto_compact_disabled_until(state):
        return True

    if state.auto_compact_disabled_until:
        state.auto_compact_disabled_until = None
    return False


@dataclass(slots=True)
class SessionRuntimeState:
    """不应该塞进自然语言消息的会话状态。"""

    session_id: str
    active_task_hash: str | None = None
    candidate_task_hash: str | None = None
    task_hash_stable_count: int = 0
    latest_checkpoint_id: str | None = None
    auto_compact_failure_count: int = 0
    auto_compact_disabled_until: str | None = None
    last_auto_compact_failure_reason: str | None = None
    system_prompt_fingerprint: str | None = None
    last_compaction_input_fingerprint: str | None = None

    def observe_task_hash_candidate(
        self,
        candidate_hash: str,
        *,
        required_stable_count: int = 2,
    ) -> bool:
        """观察候选 hash，稳定后切换 active hash。

        返回值表示本次观察是否确认了任务切换。这样 task boundary 工具可以把“模型建议”
        和“程序确认切换”分开，降低 hash 抖动带来的误触发。
        """

        if candidate_hash == self.active_task_hash:
            self.candidate_task_hash = None
            self.task_hash_stable_count = 0
            return False

        if candidate_hash == self.candidate_task_hash:
            self.task_hash_stable_count += 1
        else:
            self.candidate_task_hash = candidate_hash
            self.task_hash_stable_count = 1

        if self.task_hash_stable_count < required_stable_count:
            return False

        self.active_task_hash = candidate_hash
        self.candidate_task_hash = None
        self.task_hash_stable_count = 0
        return True

    def record_auto_compact_failure(
        self,
        reason: str,
        *,
        failure_limit: int = 3,
        disabled_minutes: int = 30,
    ) -> bool:
        """记录自动压缩失败，并在达到阈值后打开熔断。"""

        self.auto_compact_failure_count += 1
        self.last_auto_compact_failure_reason = reason
        if self.auto_compact_failure_count < failure_limit:
            return False

        self.auto_compact_disabled_until = _utc_after(disabled_minutes)
        return True

    def record_auto_compact_success(self) -> None:
        self.auto_compact_failure_count = 0
        self.auto_compact_disabled_until = None
        self.last_auto_compact_failure_reason = None
