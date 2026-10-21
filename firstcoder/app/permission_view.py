"""TUI 权限确认卡的渲染与用户回答解析。

回答解析分三层: "reject: <反馈>" 前缀 > 数字/英文别名映射 > 匹配 pending.options
里的具体选项 id/label。返回 None 表示无法解析, 由调用方重打选项重问。
"""

from __future__ import annotations


# 选项 id -> 展示编号的统一映射。permission_prompt_text 的确认卡编号和
# permission_choice_for_text 的数字别名都从它派生, 避免两处硬编码漂移。
_OPTION_NUMBERS = {
    "deny": 1,
    "allow_once": 2,
    "allow_always_same_scope": 3,
    "reject_with_feedback": 4,
}


def permission_choice_for_text(text: str, pending) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    raw = text.strip()
    if raw.lower().startswith(("reject:", "reject_with_feedback:")):
        # 拒绝并带反馈, 反馈原文回传给 agent。
        return f"reject_with_feedback: {raw.split(':', 1)[1].strip()}"
    aliases = {
        "no": "deny",
        "deny": "deny",
        "reject": "reject_with_feedback",
        "reject_with_feedback": "reject_with_feedback",
        "allow_once": "allow_once",
        "once": "allow_once",
        "allow": "allow_once",
        "allow_always": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    # 数字别名由统一编号映射派生, 保证和确认卡上展示的编号一致
    aliases.update({str(number): option_id for option_id, number in _OPTION_NUMBERS.items()})
    if normalized in aliases:
        return aliases[normalized]
    for option in getattr(pending, "options", []) or []:
        if normalized in {str(option.id).lower(), str(option.label).strip().lower().replace(" ", "_")}:
            return str(option.id)
    return None


def permission_options_text(pending) -> str:
    options = getattr(pending, "options", []) or []
    if not options:
        return "请回复权限选择：deny / allow_once / allow_always_same_scope"
    rendered = ", ".join(f"{option.id} ({option.label})" for option in options)
    return f"请回复权限选择：{rendered}"


def permission_prompt_text(pending) -> str:
    """渲染权限确认卡: 标题(action/target/reason) + 编号选项 + reject 反馈提示。

    展示编号取自模块级 _OPTION_NUMBERS, 与 permission_choice_for_text 的数字别名同源。
    """
    payload = getattr(pending, "payload", {}) or {}
    action = str(payload.get("action") or "")
    target = str(payload.get("target") or "")
    reason = str(payload.get("reason") or "")
    question = str(getattr(pending, "question", "") or "允许执行这个权限操作吗？")

    headline = "permission requested"
    if action and target:
        headline = f"{headline}  {action} {target}"
    elif action:
        headline = f"{headline}  {action}"
    elif target:
        headline = f"{headline}  {target}"
    lines = [headline]
    if reason:
        lines.append(f"  {reason}")
    elif not any((action, target)):
        lines.append(f"  {question}")

    options = list(getattr(pending, "options", []) or [])
    if options:
        choices: list[str] = []
        for index, option in enumerate(options, start=1):
            label = str(getattr(option, "label", "") or getattr(option, "id", ""))
            option_id = str(getattr(option, "id", ""))
            rendered = permission_option_label(label, option_id)
            choices.append(f"[{_OPTION_NUMBERS.get(option_id, index)}] {rendered}")
        lines.append("  " + "  ".join(choices))
    else:
        lines.append("  [1] deny  [2] allow once  [3] allow always")
    if isinstance(payload.get("prewrite_review"), dict):
        lines.append("  Or reply: reject: <feedback>")
    return "\n".join(lines)


def permission_option_label(label: str, option_id: str) -> str:
    normalized = (option_id or label).strip().lower().replace("_", " ")
    aliases = {
        "deny": "deny",
        "allow once": "allow once",
        "allow always same scope": "allow always",
        "reject with feedback": "reject: <feedback>",
    }
    return aliases.get(normalized, label.strip().lower() or option_id)
