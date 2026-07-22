"""分享文本脱敏。

这不是完整 DLP 系统，只提供第一版保守规则，避免默认 transcript/share 直接暴露
常见 secret 和本地绝对路径。
"""

from __future__ import annotations

import re

from firstcoder.session.models import RedactionOptions

SECRET_VALUE = "[REDACTED_SECRET]"
PATH_VALUE = "[REDACTED_PATH]"

_SECRET_KEY = r"[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|COOKIE)[A-Za-z0-9_]*"
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"\b(?P<key>{_SECRET_KEY})(?P<sep>\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s,\"';]+)(?P=quote)",
    re.IGNORECASE,
)
_JSON_SECRET_RE = re.compile(
    rf"(?P<prefix>[\"'](?P<key>{_SECRET_KEY})[\"']\s*:\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])(?:[A-Za-z]:\\(?:[^\s<>:\"|?*\r\n]+\\)*[^\s<>:\"|?*\r\n]*)")
_POSIX_PATH_RE = re.compile(r"(?<![\w])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")


def redact_text(text: str, options: RedactionOptions | None = None) -> str:
    """按选项脱敏普通文本。"""

    resolved = options or RedactionOptions()
    result = text
    if resolved.redact_secrets:
        result = _redact_secrets(result)
    if resolved.redact_paths:
        result = _redact_paths(result)
    return result


def _redact_secrets(text: str) -> str:
    text = _JSON_SECRET_RE.sub(lambda match: f"{match.group('prefix')}{match.group('quote')}{SECRET_VALUE}{match.group('quote')}", text)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}{SECRET_VALUE}", text)


def _redact_paths(text: str) -> str:
    text = _WINDOWS_PATH_RE.sub(PATH_VALUE, text)
    return _POSIX_PATH_RE.sub(PATH_VALUE, text)
