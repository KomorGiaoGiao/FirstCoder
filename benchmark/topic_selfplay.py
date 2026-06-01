"""任务边界 hash self-play benchmark。

这个脚本故意独立于 FirstCoder 主代码：它只验证“模型能否稳定判断任务边界，
以及代码能否根据判断结果维护 topic hash”。User Simulator 和 Topic Tracker
可以分别使用不同的 OpenAI-compatible API。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


TModel = TypeVar("TModel", bound=BaseModel)
HASH_PATTERN = re.compile(r"^ctx-[0-9a-f]{8,12}$")


class ToolCallModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class UserSimulatorOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""
    gold_decision: Literal["continue", "new_task"]
    intent: str
    hidden_reason: str
    tool_calls: list[ToolCallModel] = Field(default_factory=list)


class TrackerOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["continue", "new_task"]
    topic_hash: str
    reason: str
    reply: str = ""
    tool_calls: list[ToolCallModel] = Field(default_factory=list)

    @field_validator("topic_hash")
    @classmethod
    def validate_topic_hash(cls, value: str) -> str:
        if not HASH_PATTERN.match(value):
            raise ValueError("topic_hash must match ctx-[0-9a-f]{8,12}")
        return value


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["continue", "new_task"]
    agrees_with_simulator: bool
    reason: str

USER_SIMULATOR_SYSTEM_PROMPT = """你是一个 benchmark 的用户模拟器。

你要扮演一个真实但有点跳跃的大学生用户：你会做课程作业、写代码、赶论文、准备考试、计划旅行、处理社团事务、问生活问题，也会突然闲聊。
你的任务是生成下一轮用户消息，并给 benchmark runner 一个隐藏标准答案。
你不是 topic/hash 判断器；你只负责模拟用户。

你可以先请求读取测试沙箱里的文件，再决定下一条用户消息。这样能模拟真实用户看见项目文件后突然追问、
贴近文件内容提需求，或者从一个文件任务跳到另一个任务。

只输出 JSON，不要输出 markdown，不要输出解释：
{
  "message": "用户下一轮会说的话",
  "gold_decision": "continue" 或 "new_task",
  "intent": "continue_discussion | phase_shift_discussion_to_implementation | implementation_followup | unrelated_task | return_to_previous_topic | casual_chat | clarification",
  "hidden_reason": "为什么这个 gold_decision 是这样",
  "tool_calls": [
    {
      "name": "list_files | read_file",
      "arguments": {}
    }
  ]
}

判定规则：
- 如果用户继续推进当前任务目标，gold_decision=continue。
- 如果用户只是追问上一轮内容，例如“这个是什么意思”“怎么量化”“继续刚刚的”，gold_decision=continue。
- 如果从架构讨论切到代码实现，gold_decision=new_task。
- 如果从代码实现切到同一实现任务的测试修复，gold_decision=continue。
- 如果切换到无关任务或闲聊，gold_decision=new_task。
- 如果用户明确要求回到之前的旧主题，第一版 benchmark 仍记为 new_task，因为 runner 只维护当前 active topic，不维护 topic stack。

行为风格：
- 不要按固定比例机械切换话题，也不要显得像在写测试数据。
- 像真实大学生一样，频繁但自然地切换上下文：有时连续追一个任务，有时突然换到完全无关的事。
- 不要让长段对话一直围绕同一个项目；如果已经连续多轮延续同一任务，可以更大胆地临时想起别的作业、生活问题或文件任务。
- 不要总用“那先别讲概念了”“继续这个”“很好”这类固定开头。
- 每轮消息都要自然，任务不要雷同。
- 你可以适当突然切换话题，例如从代码作业跳到论文、从旅行计划跳到考试复习、从闲聊跳到数据分析；这种切换应该自然发生，而不是说明“我现在切换话题”。
- 不要为了延续而延续。如果当前任务已经问了几轮，可以像真实用户一样临时想起另一个更急的任务。
- 可以问：Python/JavaScript/Excel/R/论文结构/英文邮件/考试复习/租房/旅行/健身/社团海报/读书笔记/情绪闲聊/课程项目 bug。
- 有些消息可以要求助手在测试沙箱里写 Python 小脚本、修 bug、补测试或读取已有文件。
- 如果你切换任务，应该像真实用户一样直接切，不需要声明“我要切换话题”。

工具规则：
- list_files arguments: {"path": "."}
- read_file arguments: {"path": "relative/path.py"}
- 你只能读取或列出文件，不能写文件，不能运行代码。
- 如果你需要先看看沙箱里有什么文件，message 可以先留空并请求 tool_calls。
- 如果输入里的 encourage_file_reading=true，你应该更积极地先 list_files 或 read_file，但仍要保持最终用户消息自然。
- 收到工具结果后，必须输出最终 message、gold_decision、intent、hidden_reason，通常不要继续调用工具。
"""


AGENT_TRACKER_SYSTEM_PROMPT = """你是一个会维护任务 hash 的 coding agent。

你会收到当前 active topic hash、当前任务摘要、上一轮用户消息和新的用户消息。
你需要同时做两件事：
1. 判断新的用户消息是否仍属于当前 active task，并维护 topic_hash。
2. 像真实助手一样回复用户。必要时可以请求调用测试沙箱工具。

只输出 JSON，不要输出 markdown，不要输出解释：
{
  "decision": "continue" 或 "new_task",
  "topic_hash": "ctx-xxxxxxxx",
  "reason": "一句话说明边界判断",
  "reply": "给用户看的正常回复。如果需要先调用工具，这里可以简短说明动作。",
  "tool_calls": [
    {
      "name": "write_file | read_file | list_files | run_python",
      "arguments": {}
    }
  ]
}

hash 规则：
- continue 必须沿用 current_topic_hash。
- new_task 必须生成一个新的 hash，格式必须是 ctx-[0-9a-f]{8,12}，且不能等于 current_topic_hash。
- 架构讨论 -> 继续追问架构细节，算 continue。
- 架构讨论 -> 开始写代码实现，算 new_task。
- 代码实现 -> 修同一实现任务的测试，算 continue。
- 删除/修改同一任务相关文档，如果服务于当前任务，算 continue。
- 闲聊或无关任务，算 new_task。
- 不确定时偏向 new_task，避免错误合并不同任务。
- 注意渐进式陷阱：不要只看最近两次对话是否相似。连续多轮每一步都可能看似相关，但整体目标可能已经从原任务逐渐漂移到新任务；如果当前请求已经不再服务于 current_active_task_summary，应判定为 new_task。

工具规则：
- 所有文件路径都相对于测试沙箱根目录。
- write_file arguments: {"path": "relative/path.py", "content": "..."}
- read_file arguments: {"path": "relative/path.py"}
- list_files arguments: {"path": "."}
- run_python arguments: {"path": "relative/path.py"}
- 如果需要写代码或验证代码，优先使用工具。
- 一轮最多请求少量工具。看到工具结果后，给出最终 reply，通常不要继续调用工具。
"""


JUDGE_SYSTEM_PROMPT = """你是 benchmark 的独立裁判。

你会看到当前 active task 摘要、上一轮用户消息、新用户消息，以及 User Simulator 给出的 gold_decision。
你的任务是审核这个 gold_decision 是否合理。

只输出 JSON，不要输出 markdown，不要输出解释：
{
  "decision": "continue" 或 "new_task",
  "agrees_with_simulator": true 或 false,
  "reason": "一句话说明"
}

判定规则：
- 如果新用户消息继续推进当前任务目标，decision=continue。
- 如果只是追问上一轮概念或要求继续讨论，decision=continue。
- 如果从架构讨论切到代码实现，decision=new_task。
- 如果从代码实现切到同一实现任务的测试修复，decision=continue。
- 如果切换到无关任务或闲聊，decision=new_task。
- 如果用户明确要求回到之前的旧主题，第一版 benchmark 仍记为 new_task，因为 runner 只维护当前 active topic，不维护 topic stack。
- 不确定时偏向 new_task。
"""


@dataclass(slots=True)
class APIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_retries: int = 3
    request_timeout: float = 180.0
    structured_mode: str = "auto"


@dataclass(slots=True)
class RoundRecord:
    round: int
    current_task_summary: str
    previous_hash: str
    user_message: str
    simulator_gold_decision: str
    simulator_intent: str
    simulator_hidden_reason: str
    judge_decision: str
    judge_agrees_with_simulator: bool | None
    judge_reason: str
    user_tool_events: list[dict[str, Any]]
    tracker_decision: str
    tracker_hash: str
    tracker_reason: str
    agent_reply: str
    tool_events: list[dict[str, Any]]
    error: str
    decision_correct: bool
    hash_format_ok: bool
    hash_behavior_correct: bool
    raw_simulator: str
    raw_judge: str
    raw_tracker: str


class ChatClient:
    """最小 OpenAI-compatible chat completions client。"""

    def __init__(self, config: APIConfig) -> None:
        self.config = config

    def complete_json(self, *, system: str, user: str, max_tokens: int = 700) -> tuple[dict[str, Any], str]:
        return self.complete_json_messages(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )

    def complete_json_messages(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = 900,
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
        }
        # OpenAI 和多数兼容服务支持 json_object；不支持时会报错，下面会自动重试。
        payload_with_json_mode = {**payload, "response_format": {"type": "json_object"}}
        try:
            raw = self._post_chat(payload_with_json_mode)
        except RuntimeError as exc:
            if "response_format" not in str(exc):
                raise
            raw = self._post_chat(payload)

        content = self._extract_content(raw)
        return parse_json_object(content), content

    def complete_structured_messages(
        self,
        *,
        messages: list[dict[str, str]],
        output_model: type[TModel],
        tool_name: str,
        max_tokens: int = 900,
    ) -> tuple[TModel, str]:
        schema = output_model.model_json_schema()
        if self.config.structured_mode in {"auto", "tool"}:
            payload: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": "Submit the required structured benchmark response.",
                            "parameters": schema,
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": tool_name}},
            }
            try:
                raw_response = self._post_chat(payload)
                arguments, raw_text = self._extract_tool_arguments(raw_response, tool_name)
                if arguments is not None:
                    return output_model.model_validate(arguments), raw_text
            except (RuntimeError, ValidationError, ValueError, json.JSONDecodeError):
                if self.config.structured_mode == "tool":
                    raise

        if self.config.structured_mode in {"auto", "schema"}:
            payload_with_json_schema: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": tool_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
            }
            try:
                raw_response = self._post_chat(payload_with_json_schema)
                content = self._extract_content(raw_response)
                return output_model.model_validate(parse_json_object(content)), content
            except (RuntimeError, ValidationError, ValueError, json.JSONDecodeError):
                if self.config.structured_mode == "schema":
                    raise

        fallback_json, fallback_raw = self.complete_json_messages(messages=messages, max_tokens=max_tokens)
        return output_model.model_validate(fallback_json), fallback_raw

    def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        base = self.config.base_url.rstrip("/")
        url = f"{base}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code < 500 and exc.code != 429:
                    raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
                last_error = RuntimeError(f"HTTP {exc.code} from {url}: {detail}")
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = exc

            if attempt < self.config.max_retries:
                time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"request failed after {self.config.max_retries} attempts for {url}: {last_error}")

    @staticmethod
    def _extract_content(response: dict[str, Any]) -> str:
        try:
            return str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected chat response shape: {response}") from exc

    @staticmethod
    def _extract_tool_arguments(response: dict[str, Any], tool_name: str) -> tuple[dict[str, Any] | None, str]:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected chat response shape: {response}") from exc
        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if function.get("name") != tool_name:
                continue
            raw_arguments = function.get("arguments", "{}")
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            if not isinstance(arguments, dict):
                raise ValueError(f"tool arguments must be object, got {type(arguments).__name__}")
            return arguments, json.dumps(arguments, ensure_ascii=False)
        content = message.get("content")
        return None, str(content or "")


def parse_json_object(text: str) -> dict[str, Any]:
    """解析模型 JSON 输出；允许模型偶尔包一层文本。"""

    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        candidates = parse_json_object_candidates(stripped)
        if not candidates:
            raise
        value = select_protocol_object(candidates)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object, got {type(value).__name__}")
    return value


def parse_json_object_candidates(text: str) -> list[dict[str, Any]]:
    """从模型输出里尽量提取多个顶层 JSON object。

    一些模型会先输出一个裸 tool call，例如 {"name": "..."}，紧接着再输出最终协议对象。
    benchmark 真正需要的是带 message/decision/topic_hash 等协议字段的对象。
    """

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start == -1:
            break
        try:
            value, offset = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            candidates.append(value)
        cursor = start + max(offset, 1)
    return candidates


def select_protocol_object(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    protocol_keys = {"message", "decision", "topic_hash", "gold_decision", "agrees_with_simulator"}
    for candidate in candidates:
        if protocol_keys.intersection(candidate):
            return candidate
    return candidates[0]


def new_topic_hash() -> str:
    """代码生成 hash，避免不同模型输出格式漂移。"""

    return f"ctx-{secrets.token_hex(4)}"


def normalize_decision(value: Any) -> str:
    decision = str(value).strip().lower()
    if decision not in {"continue", "new_task"}:
        raise ValueError(f"invalid decision: {value!r}")
    return decision


def build_simulator_input(
    *,
    round_index: int,
    current_task_summary: str,
    previous_user_message: str,
    previous_hash: str,
    encourage_file_reading: bool,
) -> str:
    return json.dumps(
        {
            "round": round_index,
            "current_active_task_summary": current_task_summary,
            "previous_user_message": previous_user_message,
            "current_topic_hash": previous_hash,
            "encourage_file_reading": encourage_file_reading,
            "instruction": "生成下一条用户消息和隐藏 gold_decision。不要让消息里暴露 gold_decision。",
        },
        ensure_ascii=False,
        indent=2,
    )


def build_tracker_input(
    *,
    current_task_summary: str,
    previous_user_message: str,
    new_user_message: str,
    current_hash: str,
) -> str:
    return json.dumps(
        {
            "current_topic_hash": current_hash,
            "current_active_task_summary": current_task_summary,
            "previous_user_message": previous_user_message,
            "new_user_message": new_user_message,
        },
        ensure_ascii=False,
        indent=2,
    )


def build_tool_observation_input(tool_events: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "tool_observations": tool_events,
            "instruction": "根据工具结果给出最终回复。保留同一个 decision 和 topic_hash；除非绝对必要，不要继续调用工具。",
        },
        ensure_ascii=False,
        indent=2,
    )


def build_judge_input(
    *,
    current_task_summary: str,
    previous_user_message: str,
    new_user_message: str,
    simulator_gold_decision: str,
    simulator_intent: str,
    simulator_hidden_reason: str,
) -> str:
    return json.dumps(
        {
            "current_active_task_summary": current_task_summary,
            "previous_user_message": previous_user_message,
            "new_user_message": new_user_message,
            "simulator_gold_decision": simulator_gold_decision,
            "simulator_intent": simulator_intent,
            "simulator_hidden_reason": simulator_hidden_reason,
        },
        ensure_ascii=False,
        indent=2,
    )


def validate_hash_behavior(gold_decision: str, previous_hash: str, tracker_hash: str) -> tuple[bool, bool]:
    format_ok = bool(HASH_PATTERN.match(tracker_hash))
    if gold_decision == "continue":
        return format_ok, tracker_hash == previous_hash
    return format_ok, format_ok and tracker_hash != previous_hash


def update_task_summary(current: str, user_message: str, decision: str) -> str:
    """benchmark 内部的轻量任务摘要。

    这里不调用模型总结，避免引入第三个变量。new_task 时用用户消息刷新摘要；
    continue 时保留原摘要并附加一小段最近意图。
    """

    compact_message = user_message.replace("\n", " ").strip()
    if len(compact_message) > 120:
        compact_message = compact_message[:117] + "..."
    if decision == "new_task":
        return compact_message
    if compact_message and compact_message not in current:
        return f"{current} / latest: {compact_message}"[:240]
    return current


class Sandbox:
    """给 benchmark agent 使用的极小测试沙箱。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def execute_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for index, call in enumerate(calls, start=1):
            name = str(call.get("name", "")).strip()
            arguments = call.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = self._execute_one(name, arguments)
                events.append({"index": index, "name": name, "arguments": arguments, "ok": True, "result": result})
            except Exception as exc:  # noqa: BLE001 - benchmark transcript should capture tool failures.
                events.append({"index": index, "name": name, "arguments": arguments, "ok": False, "error": str(exc)})
        return events

    def _execute_one(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "write_file":
            path = self._resolve(str(arguments.get("path", "")))
            content = str(arguments.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"path": self._display(path), "bytes": len(content.encode("utf-8"))}
        if name == "read_file":
            path = self._resolve(str(arguments.get("path", "")))
            return {"path": self._display(path), "content": path.read_text(encoding="utf-8")}
        if name == "list_files":
            path = self._resolve(str(arguments.get("path", ".")))
            files = [self._display(item) for item in sorted(path.rglob("*")) if item.is_file()]
            return {"path": self._display(path), "files": files[:200], "truncated": len(files) > 200}
        if name == "run_python":
            path = self._resolve(str(arguments.get("path", "")))
            completed = subprocess.run(
                [sys.executable, str(path)],
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return {
                "path": self._display(path),
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
            }
        raise ValueError(f"unknown tool: {name}")

    def _resolve(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("path is required")
        path = (self.root / raw_path).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"path escapes sandbox: {raw_path}")
        return path

    def _display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)


def seed_sandbox(sandbox: Sandbox) -> None:
    """给 self-play 准备一点可读写材料，让用户模拟器能基于文件自然提问。"""

    fixtures = {
        "coursework/stats_homework.py": """import statistics

scores = [82, 91, 76, 88, 95, 67]

print("mean", statistics.mean(scores))
print("median", statistics.median(scores))
""",
        "coursework/readme.md": """# 课程作业草稿

- stats_homework.py: 统计课小作业
- essay_outline.md: 英语论文大纲
- todo.md: 这周要处理的杂事
""",
        "coursework/essay_outline.md": """# Should universities require attendance?

1. Introduction
2. Student autonomy
3. Classroom interaction
4. Counterargument
5. Conclusion
""",
        "notes/todo.md": """# Todo

- 给社团活动写一封英文通知邮件
- 修一下 Python 作业里输出太乱的问题
- 查周末去上海的高铁和住宿
- 复习数据库期末的范式和索引
""",
        "scripts/budget.py": """items = {
    "train": 318,
    "hotel": 420,
    "food": 180,
}

print(sum(items.values()))
""",
    }
    for relative, content in fixtures.items():
        path = sandbox.root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, ToolCallModel) for item in value):
        return [item.model_dump() for item in value]
    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            calls.append(item)
    return calls


def run_agent_tracker(
    *,
    client: ChatClient,
    sandbox: Sandbox,
    tracker_input: str,
    max_tool_rounds: int,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    messages = [
        {"role": "system", "content": AGENT_TRACKER_SYSTEM_PROMPT},
        {"role": "user", "content": tracker_input},
    ]
    all_events: list[dict[str, Any]] = []
    final_json: dict[str, Any] = {}
    final_raw = ""
    for _ in range(max_tool_rounds + 1):
        final_model, final_raw = client.complete_structured_messages(
            messages=messages,
            output_model=TrackerOutput,
            tool_name="submit_tracker_output",
            max_tokens=1400,
        )
        final_json = final_model.model_dump()
        calls = normalize_tool_calls(final_model.tool_calls)
        if not calls:
            return final_json, final_raw, all_events
        events = sandbox.execute_calls(calls)
        all_events.extend(events)
        messages.append({"role": "assistant", "content": final_raw})
        messages.append({"role": "user", "content": build_tool_observation_input(events)})
    return final_json, final_raw, all_events


def build_user_tool_observation_input(tool_events: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "tool_observations": tool_events,
            "instruction": "根据这些只读工具结果，生成最终用户消息和隐藏 gold_decision。不要继续调用工具，除非确实还缺一个关键文件。",
        },
        ensure_ascii=False,
        indent=2,
    )


def run_user_simulator(
    *,
    client: ChatClient,
    sandbox: Sandbox,
    simulator_input: str,
    max_tool_rounds: int,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    messages = [
        {"role": "system", "content": USER_SIMULATOR_SYSTEM_PROMPT},
        {"role": "user", "content": simulator_input},
    ]
    all_events: list[dict[str, Any]] = []
    final_json: dict[str, Any] = {}
    final_raw = ""
    for _ in range(max_tool_rounds + 1):
        final_model, final_raw = client.complete_structured_messages(
            messages=messages,
            output_model=UserSimulatorOutput,
            tool_name="submit_user_simulator_output",
            max_tokens=1200,
        )
        final_json = final_model.model_dump()
        calls = normalize_user_tool_calls(final_model.tool_calls)
        if not calls:
            return final_json, final_raw, all_events
        events = sandbox.execute_calls(calls)
        all_events.extend(events)
        messages.append({"role": "assistant", "content": final_raw})
        messages.append({"role": "user", "content": build_user_tool_observation_input(events)})
    return final_json, final_raw, all_events


def normalize_user_tool_calls(value: Any) -> list[dict[str, Any]]:
    calls = []
    for call in normalize_tool_calls(value):
        name = str(call.get("name", "")).strip()
        if name in {"list_files", "read_file"}:
            calls.append(call)
    return calls


def run_benchmark(args: argparse.Namespace) -> list[RoundRecord]:
    user_client = ChatClient(
        APIConfig(
            base_url=args.user_api_base,
            api_key=args.user_api_key,
            model=args.user_model,
            temperature=args.user_temperature,
            max_retries=args.max_retries,
            request_timeout=args.request_timeout,
            structured_mode=args.structured_mode,
        )
    )
    tracker_client = ChatClient(
        APIConfig(
            base_url=args.tracker_api_base,
            api_key=args.tracker_api_key,
            model=args.tracker_model,
            temperature=args.tracker_temperature,
            max_retries=args.max_retries,
            request_timeout=args.request_timeout,
            structured_mode=args.structured_mode,
        )
    )
    judge_client = None
    if args.judge_model:
        judge_client = ChatClient(
            APIConfig(
                base_url=args.judge_api_base,
                api_key=args.judge_api_key,
                model=args.judge_model,
                temperature=args.judge_temperature,
                max_retries=args.max_retries,
                request_timeout=args.request_timeout,
                structured_mode=args.structured_mode,
            )
        )

    current_hash = args.initial_hash or new_topic_hash()
    current_task_summary = args.initial_task
    previous_user_message = ""
    records: list[RoundRecord] = []
    sandbox = Sandbox(args.sandbox_root)
    if args.reset_sandbox:
        sandbox.reset()
    seed_sandbox(sandbox)
    initialize_outputs(args, current_hash)

    for index in range(1, args.rounds + 1):
        simulator_input = build_simulator_input(
            round_index=index,
            current_task_summary=current_task_summary,
            previous_user_message=previous_user_message,
            previous_hash=current_hash,
            encourage_file_reading=args.encourage_user_file_reading,
        )
        simulator_json, raw_simulator, user_tool_events = run_user_simulator(
            client=user_client,
            sandbox=sandbox,
            simulator_input=simulator_input,
            max_tool_rounds=args.max_user_tool_rounds,
        )
        user_message = str(simulator_json.get("message", "")).strip()
        if not user_message:
            raise RuntimeError(f"simulator returned empty message at round {index}: {raw_simulator}")
        gold_decision = normalize_decision(simulator_json.get("gold_decision"))
        simulator_intent = str(simulator_json.get("intent", "")).strip()
        simulator_hidden_reason = str(simulator_json.get("hidden_reason", "")).strip()

        judge_decision = gold_decision
        judge_agrees: bool | None = None
        judge_reason = ""
        raw_judge = ""
        if judge_client is not None:
            judge_input = build_judge_input(
                current_task_summary=current_task_summary,
                previous_user_message=previous_user_message,
                new_user_message=user_message,
                simulator_gold_decision=gold_decision,
                simulator_intent=simulator_intent,
                simulator_hidden_reason=simulator_hidden_reason,
            )
            judge_model, raw_judge = judge_client.complete_structured_messages(
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": judge_input},
                ],
                output_model=JudgeOutput,
                tool_name="submit_judge_output",
            )
            judge_decision = judge_model.decision
            judge_agrees = judge_model.agrees_with_simulator
            judge_reason = judge_model.reason.strip()

        tool_events: list[dict[str, Any]] = []
        error = ""
        raw_tracker = ""
        try:
            tracker_input = build_tracker_input(
                current_task_summary=current_task_summary,
                previous_user_message=previous_user_message,
                new_user_message=user_message,
                current_hash=current_hash,
            )
            tracker_json, raw_tracker, tool_events = run_agent_tracker(
                client=tracker_client,
                sandbox=sandbox,
                tracker_input=tracker_input,
                max_tool_rounds=args.max_tool_rounds,
            )
            tracker_decision = normalize_decision(tracker_json.get("decision"))
            tracker_hash = str(tracker_json.get("topic_hash", "")).strip()
            tracker_reason = str(tracker_json.get("reason", "")).strip()
            agent_reply = str(tracker_json.get("reply", "")).strip()
            hash_format_ok, hash_behavior_correct = validate_hash_behavior(
                judge_decision,
                current_hash,
                tracker_hash,
            )
            decision_correct = tracker_decision == judge_decision
        except Exception as exc:  # noqa: BLE001 - benchmark should record model failures.
            tracker_decision = "format_error"
            tracker_hash = ""
            tracker_reason = ""
            agent_reply = ""
            hash_format_ok = False
            hash_behavior_correct = False
            decision_correct = False
            error = f"{type(exc).__name__}: {exc}"
        record = RoundRecord(
            round=index,
            current_task_summary=current_task_summary,
            previous_hash=current_hash,
            user_message=user_message,
            simulator_gold_decision=gold_decision,
            simulator_intent=simulator_intent,
            simulator_hidden_reason=simulator_hidden_reason,
            judge_decision=judge_decision,
            judge_agrees_with_simulator=judge_agrees,
            judge_reason=judge_reason,
            user_tool_events=user_tool_events,
            tracker_decision=tracker_decision,
            tracker_hash=tracker_hash,
            tracker_reason=tracker_reason,
            agent_reply=agent_reply,
            tool_events=tool_events,
            error=error,
            decision_correct=decision_correct,
            hash_format_ok=hash_format_ok,
            hash_behavior_correct=hash_behavior_correct,
            raw_simulator=raw_simulator,
            raw_judge=raw_judge,
            raw_tracker=raw_tracker,
        )
        records.append(record)
        append_jsonl(args.out, asdict(record))
        append_transcript(args.transcript_out, record)

        if args.verbose:
            status = "OK" if decision_correct and hash_behavior_correct else "FAIL"
            print(
                f"[{status}] round={index} gold={judge_decision} "
                f"tracker={tracker_decision} hash={tracker_hash}",
                flush=True,
            )

        if judge_decision == "new_task":
            current_hash = tracker_hash if hash_format_ok and tracker_hash != current_hash else new_topic_hash()
        previous_user_message = user_message
        current_task_summary = update_task_summary(current_task_summary, user_message, judge_decision)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    return records


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def initialize_outputs(args: argparse.Namespace, initial_hash: str) -> None:
    """每次 benchmark run 都重建输出文件，并写入可读 header。"""

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.transcript_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("", encoding="utf-8")
    header = [
        "# Topic Self-Play Benchmark",
        "",
        f"- Started at: `{time.strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- Rounds: `{args.rounds}`",
        f"- Initial task: {args.initial_task}",
        f"- Initial hash: `{initial_hash}`",
        f"- User model: `{args.user_model}`",
        f"- Tracker model: `{args.tracker_model}`",
        f"- Judge model: `{args.judge_model or '(disabled)'}`",
        f"- Sandbox: `{args.sandbox_root}`",
        "",
        "---",
        "",
    ]
    args.transcript_out.write_text("\n".join(header), encoding="utf-8")


def append_transcript(path: Path, record: RoundRecord) -> None:
    """写给人肉审阅看的简洁 transcript。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"## Round {record.round}\n\n")
        file.write(f"Previous hash: `{record.previous_hash}`\n\n")
        file.write("**User**\n\n")
        file.write(record.user_message.strip() + "\n\n")
        if record.user_tool_events:
            file.write("**User Read Tools**\n\n")
            for event in record.user_tool_events:
                file.write(f"- `{event.get('name')}` ok=`{str(event.get('ok')).lower()}`\n")
                if event.get("ok"):
                    file.write("  - result: `" + compact_for_markdown(event.get("result")) + "`\n")
                else:
                    file.write("  - error: `" + compact_for_markdown(event.get("error")) + "`\n")
            file.write("\n")
        file.write("**Agent**\n\n")
        file.write(f"`{record.tracker_hash}`\n\n")
        file.write((record.agent_reply or "(empty reply)").strip() + "\n\n")
        if record.tool_events:
            file.write("**Tools**\n\n")
            for event in record.tool_events:
                file.write(f"- `{event.get('name')}` ok=`{str(event.get('ok')).lower()}`\n")
                if event.get("ok"):
                    file.write("  - result: `" + compact_for_markdown(event.get("result")) + "`\n")
                else:
                    file.write("  - error: `" + compact_for_markdown(event.get("error")) + "`\n")
            file.write("\n")
        file.write("**Boundary Decision**\n\n")
        file.write(f"- Decision: `{record.tracker_decision}`\n")
        file.write(f"- Reason: {record.tracker_reason or '(empty)'}\n\n")
        file.write("**Judge**\n\n")
        file.write(f"- Decision: `{record.judge_decision}`\n")
        if record.judge_agrees_with_simulator is not None:
            file.write(f"- Agrees with simulator: `{str(record.judge_agrees_with_simulator).lower()}`\n")
        file.write(f"- Reason: {record.judge_reason or '(empty)'}\n\n")
        file.write("**Score**\n\n")
        file.write(f"- Decision correct: `{str(record.decision_correct).lower()}`\n")
        file.write(f"- Hash behavior correct: `{str(record.hash_behavior_correct).lower()}`\n\n")
        if record.error:
            file.write("**Error**\n\n")
            file.write(f"`{compact_for_markdown(record.error)}`\n\n")
        file.write("---\n\n")


def compact_for_markdown(value: Any, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", "\\n").replace("`", "'")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def summarize(records: list[RoundRecord]) -> dict[str, Any]:
    total = len(records)
    if total == 0:
        return {"rounds": 0}
    false_continue = sum(
        1
        for record in records
        if record.judge_decision == "new_task" and record.tracker_decision == "continue"
    )
    false_new_task = sum(
        1
        for record in records
        if record.judge_decision == "continue" and record.tracker_decision == "new_task"
    )
    judged = [record for record in records if record.judge_agrees_with_simulator is not None]
    return {
        "rounds": total,
        "decision_accuracy": ratio(sum(record.decision_correct for record in records), total),
        "hash_format_accuracy": ratio(sum(record.hash_format_ok for record in records), total),
        "hash_behavior_accuracy": ratio(sum(record.hash_behavior_correct for record in records), total),
        "false_continue": false_continue,
        "false_new_task": false_new_task,
        "format_errors": sum(not record.hash_format_ok for record in records),
        "runtime_errors": sum(bool(record.error) for record in records),
        "judge_enabled": bool(judged),
        "simulator_judge_agreement": (
            ratio(sum(record.judge_agrees_with_simulator is True for record in judged), len(judged))
            if judged
            else None
        ),
    }


def ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run topic boundary self-play benchmark.")
    parser.add_argument("--rounds", type=int, default=int(env_or_default("TOPIC_BENCH_ROUNDS", "20")))
    parser.add_argument("--initial-task", default="正在和助手协作完成一个任务")
    parser.add_argument("--initial-hash", default="")
    parser.add_argument("--out", type=Path, default=Path("benchmark/runs/topic_selfplay.jsonl"))
    parser.add_argument("--transcript-out", type=Path, default=Path("benchmark/runs/topic_selfplay.md"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--max-retries", type=int, default=int(env_or_default("TOPIC_BENCH_MAX_RETRIES", "3")))
    parser.add_argument("--request-timeout", type=float, default=float(env_or_default("TOPIC_BENCH_REQUEST_TIMEOUT", "180")))
    parser.add_argument(
        "--structured-mode",
        choices=["auto", "tool", "schema", "json"],
        default=env_or_default("TOPIC_BENCH_STRUCTURED_MODE", "auto"),
    )
    parser.add_argument("--sandbox-root", type=Path, default=Path("benchmark/sandbox"))
    parser.add_argument("--reset-sandbox", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-tool-rounds", type=int, default=2)
    parser.add_argument("--max-user-tool-rounds", type=int, default=1)
    parser.add_argument("--encourage-user-file-reading", action="store_true")

    default_base = env_or_default("OPENAI_API_BASE", "https://api.openai.com/v1")
    default_key = os.environ.get("OPENAI_API_KEY", "")

    parser.add_argument("--user-api-base", default=env_or_default("USER_API_BASE", default_base))
    parser.add_argument("--user-api-key", default=env_or_default("USER_API_KEY", default_key))
    parser.add_argument("--user-model", default=env_or_default("USER_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--user-temperature", type=float, default=float(env_or_default("USER_TEMPERATURE", "0.5")))

    parser.add_argument("--tracker-api-base", default=env_or_default("TRACKER_API_BASE", default_base))
    parser.add_argument("--tracker-api-key", default=env_or_default("TRACKER_API_KEY", default_key))
    parser.add_argument("--tracker-model", default=env_or_default("TRACKER_MODEL", "gpt-5-mini"))
    parser.add_argument("--tracker-temperature", type=float, default=float(env_or_default("TRACKER_TEMPERATURE", "0.1")))

    parser.add_argument("--judge-api-base", default=env_or_default("JUDGE_API_BASE", default_base))
    parser.add_argument("--judge-api-key", default=env_or_default("JUDGE_API_KEY", default_key))
    parser.add_argument("--judge-model", default=env_or_default("JUDGE_MODEL", ""))
    parser.add_argument("--judge-temperature", type=float, default=float(env_or_default("JUDGE_TEMPERATURE", "0.0")))
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.rounds <= 0:
        raise SystemExit("--rounds must be positive")
    missing = []
    if not args.user_api_key:
        missing.append("--user-api-key or USER_API_KEY/OPENAI_API_KEY")
    if not args.tracker_api_key:
        missing.append("--tracker-api-key or TRACKER_API_KEY/OPENAI_API_KEY")
    if args.judge_model and not args.judge_api_key:
        missing.append("--judge-api-key or JUDGE_API_KEY/OPENAI_API_KEY")
    if missing:
        raise SystemExit("Missing required API credentials: " + ", ".join(missing))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args)
    records = run_benchmark(args)
    summary = summarize(records)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
