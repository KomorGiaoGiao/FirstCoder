"""Structured user-input requests shared across permissions, tools, and agent.

`ask_user` and permission confirmation share one request shape. The `kind` field
separates semantics so models cannot disguise a permission prompt as a normal
question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from firstcoder.tools.types import ToolResult


@dataclass(slots=True)
class UserInputOption:
    """One answer choice shown to the user."""

    id: str
    label: str
    description: str = ""


@dataclass(slots=True)
class UserInputRequest:
    """Structured pause that requires a human answer before the turn continues."""

    id: str
    kind: Literal["ask_user", "permission_confirmation"]
    question: str
    options: list[UserInputOption] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


def user_input_request_from_tool_result(
    result: ToolResult,
    *,
    tool_call_id: str,
    tool_name: str,
) -> UserInputRequest | None:
    """Rebuild a user-input request from structured tool-result data."""

    data = getattr(result, "data", {}) or {}
    if not data.get("requires_user_input"):
        return None

    request_type = str(data.get("request_type") or "ask_user")
    if request_type not in {"ask_user", "permission_confirmation"}:
        request_type = "ask_user"

    content = str(getattr(result, "content", "") or "")
    question = str(data.get("question") or content).strip()
    if not question:
        question = "需要用户输入。"

    options = _options_from_data(data.get("options"))
    request_id = str(data.get("request_id") or data.get("permission_request_id") or tool_call_id)
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_result_name": getattr(result, "name", tool_name),
    }
    for key in ("request_type", "permission_request_id", "permission_request", "pending_tool_call"):
        if key in data:
            payload[key] = data[key]

    return UserInputRequest(
        id=request_id,
        kind=request_type,  # type: ignore[arg-type]
        question=question,
        options=options,
        payload=payload,
    )


def _options_from_data(raw_options: object) -> list[UserInputOption]:
    if not isinstance(raw_options, list):
        return []

    options: list[UserInputOption] = []
    for index, raw_option in enumerate(raw_options, start=1):
        if isinstance(raw_option, dict):
            label = str(raw_option.get("label") or raw_option.get("id") or "").strip()
            if not label:
                continue
            option_id = str(raw_option.get("id") or index)
            description = str(raw_option.get("description") or "")
        else:
            label = str(raw_option).strip()
            if not label:
                continue
            option_id = str(index)
            description = ""
        options.append(UserInputOption(id=option_id, label=label, description=description))
    return options
