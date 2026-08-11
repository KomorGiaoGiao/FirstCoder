"""Benchmark 执行证据与最终答复门禁。

这一层只记录当前用户回合已经发生的可观察事实，不尝试判断题目是否真的通过隐藏
verifier。它的职责是阻止最常见的过早收尾：修改后完全没验证、最后一次验证失败，
或自己启动的后台任务仍未结束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from firstcoder.agent.background import STATUS_FAILED, STATUS_RUNNING, BackgroundJob
from firstcoder.providers.types import ToolCall
from firstcoder.tools.types import ToolResult

MUTATION_TOOL_NAMES = frozenset({"write", "edit", "apply_patch", "delete"})
VALIDATION_TOOL_NAMES = frozenset({"diagnostics", "review"})
PROCESS_MUTATION_TOOL_NAMES = frozenset({"process_start", "process_stop"})

_VALIDATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:"
    r"pytest|python\s+-m\s+(?:pytest|unittest)|unittest|"
    r"cargo\s+(?:test|check|build)|go\s+(?:test|vet|build)|"
    r"npm\s+(?:test|run\s+(?:test|lint|check|build))|"
    r"pnpm\s+(?:test|lint|check|build)|yarn\s+(?:test|lint|check|build)|"
    r"ruff(?:\s+check)?|mypy|pyright|tsc|eslint|"
    r"make\s+(?:test|check|lint)|cmake\s+--build|"
    r"curl|wget"
    r")(?:\s|$)",
    re.IGNORECASE,
)
_BROAD_VALIDATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:"
    r"pytest|python\s+-m\s+(?:pytest|unittest)|unittest|"
    r"cargo\s+(?:test|check|build)|go\s+(?:test|vet|build)|"
    r"npm\s+(?:test|run\s+(?:test|lint|check|build))|"
    r"pnpm\s+(?:test|lint|check|build)|yarn\s+(?:test|lint|check|build)|"
    r"ruff(?:\s+check)?|mypy|pyright|tsc|eslint|"
    r"make\s+(?:test|check|lint)|cmake\s+--build"
    r")(?:\s|$)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s'\"<>`]+", re.IGNORECASE)
_EXPECTED_HTTP_STATUS_RE = re.compile(
    r"(?:"
    r"(?:returns?|responds?(?:\s+with)?|reports?|has|is|=)\s+"
    r"(?:an?\s+)?(?:HTTP\s+)?(?:status(?:\s+code)?\s+)?"
    r"|HTTP(?:\s+status)?\s*[:=]?\s*"
    r")(?P<status>[1-5]\d{2})\b",
    re.IGNORECASE,
)
_EXPECTED_QUOTED_OUTPUT_RE = re.compile(
    r"(?:"
    r"(?:returns?|prints?|outputs?|shows?|contains?)\s+"
    r"(?:the\s+)?(?:output|content|body|text)?\s*"
    r"|(?:see|get|receive|expect)\s+(?:the\s+)?"
    r"(?:output|content|body|text)\s*(?:is|as|to\s+be|=|:)?\s*"
    r")"
    r"(?P<quote>['\"`])(?P<value>[^'\"`\r\n]{1,200})(?P=quote)",
    re.IGNORECASE,
)
_EXPECTED_UNQUOTED_OUTPUT_RE = re.compile(
    r"(?:returns?|prints?|outputs?|shows?|contains?)\s+"
    r"(?:the\s+)?(?:output|content|body|text)?\s*"
    r"(?P<value>[^\r\n.;]{1,120})",
    re.IGNORECASE,
)
_ASSERTIVE_VALIDATION_RE = re.compile(
    r"(?:"
    r"(?:^|[;&|\s])(?:test|\[|grep\s+-q|cmp|diff)(?:\s|$)|"
    r"\bassert\b|\braise\s+SystemExit\b|"
    r"\bcurl\b[^\n;&|]*(?:--fail(?:-with-body)?|-f)(?:\s|$)|"
    r"\bwget\b[^\n;&|]*(?:--spider|--server-response)(?:\s|$)|"
    r"\bexit\s+[1-9]\d*\b"
    r")",
    re.IGNORECASE,
)
_FAILED_HTTP_STATUS_RE = re.compile(
    r"(?:HTTP(?:/\S+)?(?:[=:_\s-]+)|status(?:[=:_\s-]+))([45]\d{2})\b",
    re.IGNORECASE,
)
_MUTATION_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:^|[;&|\s])(?:rm|del|erase|rmdir|mkdir|md|touch|mv|move|cp|copy|"
    r"chmod|chown|install|tee|set-content|add-content|new-item|remove-item|"
    r"move-item|copy-item|rename-item|systemctl\s+(?:start|restart|enable)|"
    r"service\s+\S+\s+(?:start|restart)|apt(?:-get)?\s+install|yum\s+install|"
    r"dnf\s+install|apk\s+add|pip\s+install|uv\s+pip\s+install|"
    r"npm\s+install|pnpm\s+install|yarn\s+add|git\s+(?:apply|checkout|restore)"
    r")(?:\s|$)"
    r"|(?:^|[^<])>>?\s*[^&|]"
    r"|\bsed\s+-i\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompletionGateDecision:
    """一次最终答复门禁的判定结果。"""

    reasons: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return bool(self.reasons)

    def render_instruction(self) -> str | None:
        if not self.reasons:
            return None
        lines = [
            "Benchmark completion check: do not finalize yet.",
            "The execution evidence still has unresolved acceptance risk:",
        ]
        lines.extend(f"- {reason}" for reason in self.reasons)
        lines.extend(
            [
                "Use the available tools now to verify the required observable outcome and fix any failure.",
                "If a concrete external blocker makes verification impossible, state that blocker precisely; "
                "do not merely repeat the intended solution or claim success without evidence.",
            ]
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ExplicitValidationExpectation:
    """题面直接声明的 HTTP 目标及其可观察预期。"""

    target: str
    expected_statuses: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()

    def missing_from_command(self, command: str) -> tuple[str, ...]:
        normalized = _normalize_validation_text(
            _final_target_validation_context(command, self.target)
        )
        missing: list[str] = []
        for status in self.expected_statuses:
            if status.casefold() not in normalized:
                missing.append(f"HTTP {status}")
        for output in self.expected_outputs:
            if _normalize_validation_text(output) not in normalized:
                missing.append(f"output {output!r}")
        return tuple(missing)

    def render(self) -> str:
        outcomes = [f"HTTP {status}" for status in self.expected_statuses]
        outcomes.extend(f"response body {output!r}" for output in self.expected_outputs)
        return f"{self.target} must preserve " + " and ".join(outcomes)


@dataclass(slots=True)
class ExecutionEvidence:
    """累计当前 benchmark 用户回合中的修改、验证和后台任务证据。"""

    sequence: int = 0
    last_mutation_sequence: int | None = None
    last_mutation_tool: str | None = None
    last_validation_sequence: int | None = None
    last_validation_tool: str | None = None
    last_validation_ok: bool | None = None
    validation_attempts: int = 0
    failed_tool_calls: int = 0
    background_job_ids: set[str] = field(default_factory=set)
    explicit_validation_targets: tuple[str, ...] = ()
    explicit_validation_expectations: tuple[ExplicitValidationExpectation, ...] = ()
    last_validation_command: str | None = None
    last_validation_assertive: bool | None = None

    @classmethod
    def for_task(cls, task: str) -> "ExecutionEvidence":
        return cls(
            explicit_validation_targets=explicit_validation_targets(task),
            explicit_validation_expectations=explicit_validation_expectations(task),
        )

    def reset(self) -> None:
        self.sequence = 0
        self.last_mutation_sequence = None
        self.last_mutation_tool = None
        self.last_validation_sequence = None
        self.last_validation_tool = None
        self.last_validation_ok = None
        self.validation_attempts = 0
        self.failed_tool_calls = 0
        self.background_job_ids.clear()
        self.last_validation_command = None
        self.last_validation_assertive = None

    def observe(self, tool_call: ToolCall, result: ToolResult) -> None:
        """记录一个已经得到最终结果的工具调用。"""

        if _is_control_result(result):
            return
        self.sequence += 1
        self._observe_background_result(result)

        validation = is_validation_call(tool_call)
        mutation = result.ok and is_mutation_result(tool_call, result)
        if mutation:
            self.last_mutation_sequence = self.sequence
            self.last_mutation_tool = tool_call.name
        if validation:
            self.validation_attempts += 1
            self.last_validation_sequence = self.sequence
            self.last_validation_tool = tool_call.name
            command = _command_text(tool_call)
            self.last_validation_command = command or None
            self.last_validation_assertive = validation_is_assertive(tool_call)
            self.last_validation_ok = result.ok and (
                self.last_validation_assertive
                or not _validation_output_reports_failure(result)
            )
        if not result.ok:
            self.failed_tool_calls += 1

    def completion_decision(
        self,
        *,
        background_jobs: Iterable[BackgroundJob] = (),
    ) -> CompletionGateDecision:
        reasons: list[str] = []
        relevant_jobs = [job for job in background_jobs if job.id in self.background_job_ids]
        running = [job.id for job in relevant_jobs if job.status == STATUS_RUNNING]
        failed = [job.id for job in relevant_jobs if job.status == STATUS_FAILED]
        if running:
            reasons.append(f"background jobs are still running: {', '.join(sorted(running))}")
        if failed:
            reasons.append(f"background jobs failed and their outcome is unresolved: {', '.join(sorted(failed))}")

        if self.last_validation_ok is False:
            tool = self.last_validation_tool or "validation tool"
            reasons.append(f"the most recent validation ({tool}) failed")
        elif self.last_mutation_sequence is not None and (
            self.last_validation_sequence is None
            or self.last_validation_sequence < self.last_mutation_sequence
        ):
            tool = self.last_mutation_tool or "mutation tool"
            reasons.append(f"the latest workspace change ({tool}) has not been validated afterwards")
        elif self.last_validation_ok and self._missing_explicit_validation_targets():
            missing = ", ".join(self._missing_explicit_validation_targets())
            reasons.append(
                "the most recent direct validation did not cover the task's explicit observable "
                f"target(s): {missing}"
            )
        elif self.last_validation_ok and self._missing_explicit_validation_expectations():
            missing = "; ".join(self._missing_explicit_validation_expectations())
            reasons.append(
                "the most recent direct validation did not assert the task's explicit expected "
                f"outcome(s): {missing}"
            )
        elif (
            self.last_validation_ok
            and self.explicit_validation_targets
            and self.last_validation_tool in {"shell", "python_exec"}
            and not self.last_validation_assertive
        ):
            reasons.append(
                "the most recent direct validation was informational only; rerun the exact task "
                "target with a failure-sensitive assertion (for example test/assert, curl --fail, "
                "and an expected-content comparison)"
            )
        return CompletionGateDecision(tuple(reasons))

    def render_acceptance_contract(self) -> str | None:
        if not self.explicit_validation_expectations:
            return None
        lines = [
            "Task-derived final acceptance contract:",
            "The user task explicitly requires these observable outcomes in the final environment:",
        ]
        lines.extend(
            f"- {expectation.render()}"
            for expectation in self.explicit_validation_expectations
        )
        lines.extend(
            [
                "Preserve these outcomes after validation and cleanup unless the task itself explicitly "
                "requires a reset or empty state.",
                "If any later command changes a listed target, rerun a failure-sensitive assertion of "
                "the listed outcome before finalizing.",
            ]
        )
        return "\n".join(lines)

    def _observe_background_result(self, result: ToolResult) -> None:
        job_id = result.data.get("background_job_id")
        if isinstance(job_id, str) and job_id:
            self.background_job_ids.add(job_id)

    def _missing_explicit_validation_targets(self) -> tuple[str, ...]:
        if not self.explicit_validation_targets:
            return ()
        if self.last_validation_tool not in {"shell", "python_exec"}:
            return ()
        command = self.last_validation_command or ""
        if _BROAD_VALIDATION_COMMAND_RE.search(command):
            return ()
        return tuple(target for target in self.explicit_validation_targets if target not in command)

    def _missing_explicit_validation_expectations(self) -> tuple[str, ...]:
        if not self.explicit_validation_expectations:
            return ()
        if self.last_validation_tool not in {"shell", "python_exec"}:
            return ()
        command = self.last_validation_command or ""
        if _BROAD_VALIDATION_COMMAND_RE.search(command):
            return ()
        missing: list[str] = []
        for expectation in self.explicit_validation_expectations:
            if expectation.target not in command:
                continue
            outcomes = expectation.missing_from_command(command)
            if outcomes:
                missing.append(f"{expectation.target} -> {', '.join(outcomes)}")
        return tuple(missing)


def is_validation_call(tool_call: ToolCall) -> bool:
    if tool_call.name in VALIDATION_TOOL_NAMES:
        return True
    if tool_call.name not in {"shell", "python_exec"}:
        return False
    command = _command_text(tool_call)
    return bool(
        command
        and (
            _VALIDATION_COMMAND_RE.search(command)
            or _ASSERTIVE_VALIDATION_RE.search(command)
        )
    )


def is_mutation_result(tool_call: ToolCall, result: ToolResult) -> bool:
    if not result.ok:
        return False
    if tool_call.name in MUTATION_TOOL_NAMES:
        return True
    if tool_call.name in PROCESS_MUTATION_TOOL_NAMES:
        return True
    if tool_call.name == "git_diff":
        return _has_real_diff(result)
    if tool_call.name not in {"shell", "python_exec"}:
        return False
    command = _command_text(tool_call)
    return bool(command and _MUTATION_COMMAND_RE.search(command))


def has_resetting_success(tool_call: ToolCall, result: ToolResult) -> bool:
    """成功执行实际修改时，旧失败链已不再代表当前状态。"""

    return tool_call.name != "git_diff" and is_mutation_result(tool_call, result)


def explicit_validation_targets(task: str) -> tuple[str, ...]:
    """提取任务中明确给出的 HTTP 可观察目标，不推断隐藏 verifier。"""

    targets: list[str] = []
    for match in _URL_RE.finditer(task):
        target = _validation_target(match.group(0))
        if target and target not in targets:
            targets.append(target)
    return tuple(targets)


def explicit_validation_expectations(
    task: str,
) -> tuple[ExplicitValidationExpectation, ...]:
    """提取题面在显式 HTTP 目标附近声明的状态码或响应正文。"""

    matches = list(_URL_RE.finditer(task))
    expectations: list[ExplicitValidationExpectation] = []
    for index, match in enumerate(matches):
        target = _validation_target(match.group(0))
        if not target:
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(task)
        context_start = max(0, match.start() - 120)
        context_end = min(next_start, match.end() + 420)
        context = task[context_start:context_end]
        statuses = _unique_matches(
            status_match.group("status")
            for status_match in _EXPECTED_HTTP_STATUS_RE.finditer(context)
        )
        outputs = _expected_outputs(context)
        if not statuses and not outputs:
            continue
        expectations.append(
            ExplicitValidationExpectation(
                target=target,
                expected_statuses=statuses,
                expected_outputs=outputs,
            )
        )
    return tuple(expectations)


def validation_is_assertive(tool_call: ToolCall) -> bool:
    if tool_call.name in VALIDATION_TOOL_NAMES:
        return True
    command = _command_text(tool_call)
    if not command:
        return False
    return bool(
        _BROAD_VALIDATION_COMMAND_RE.search(command)
        or _ASSERTIVE_VALIDATION_RE.search(command)
    )


def _has_real_diff(result: ToolResult) -> bool:
    content = result.content.strip()
    return bool(content and content != "没有 diff。")


def _validation_output_reports_failure(result: ToolResult) -> bool:
    text = "\n".join(
        str(value)
        for value in (
            result.content,
            result.data.get("stdout"),
            result.data.get("stderr"),
        )
        if value
    )
    return bool(_FAILED_HTTP_STATUS_RE.search(text))


def _expected_outputs(context: str) -> tuple[str, ...]:
    quoted = _unique_matches(
        match.group("value").strip()
        for match in _EXPECTED_QUOTED_OUTPUT_RE.finditer(context)
    )
    if quoted:
        return quoted
    values: list[str] = []
    for match in _EXPECTED_UNQUOTED_OUTPUT_RE.finditer(context):
        value = match.group("value").strip().strip("'\"`")
        if not value or _EXPECTED_HTTP_STATUS_RE.search(match.group(0)):
            continue
        values.append(value)
    return _unique_matches(values)


def _unique_matches(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return tuple(unique)


def _validation_target(raw_url: str) -> str:
    parsed = urlsplit(raw_url.rstrip(".,;:!?)]}"))
    target = parsed.path or parsed.netloc
    if parsed.query:
        target += "?" + parsed.query
    if target == "/":
        target = parsed.netloc
    return target


def _normalize_validation_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _final_target_validation_context(command: str, target: str) -> str:
    """只检查最后一次目标探测及其后续断言，避免早先成功掩盖最终清理。"""

    target_index = command.rfind(target)
    if target_index < 0:
        return command
    line_start = command.rfind("\n", 0, target_index)
    start = 0 if line_start < 0 else line_start + 1
    return command[start:]


def _command_text(tool_call: ToolCall) -> str:
    if not isinstance(tool_call.arguments, dict):
        return ""
    if tool_call.name == "python_exec":
        return str(tool_call.arguments.get("code") or "")
    return str(tool_call.arguments.get("command") or "")


def _is_control_result(result: ToolResult) -> bool:
    data = result.data
    request_type = data.get("request_type")
    return bool(
        data.get("requires_user_input")
        or data.get("skipped_due_to_user_input")
        or data.get("interrupted")
        or data.get("stagnation_blocked")
        or (
            isinstance(request_type, str)
            and request_type.startswith(("permission_", "prewrite_review_"))
        )
    )
