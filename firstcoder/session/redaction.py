"""分享文本脱敏。

这不是完整 DLP 系统，只提供第一版保守规则，避免默认 transcript/share 直接暴露
常见 secret 和本地绝对路径。
"""

from __future__ import annotations

import re

from firstcoder.session.models import RedactionOptions

SECRET_VALUE = "[REDACTED_SECRET]"
PATH_VALUE = "[REDACTED_PATH]"

# 键名特征: 含 KEY/TOKEN/SECRET/PASSWORD/COOKIE 字样即视为敏感, 如 API_KEY, access_token
_SECRET_KEY = r"[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|COOKIE)[A-Za-z0-9_]*"
# 匹配赋值语句: API_KEY=sk-xxx 或 password: "abc123", 只脱敏值部分, 保留键名便于定位
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"\b(?P<key>{_SECRET_KEY})(?P<sep>\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s,\"';]+)(?P=quote)",
    re.IGNORECASE,
)
# 匹配 JSON 键值对: {"access_token": "abc123"}, 值可含空格, 保留键名和引号结构
_JSON_SECRET_RE = re.compile(
    rf"(?P<prefix>[\"'](?P<key>{_SECRET_KEY})[\"']\s*:\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
# Windows 绝对路径: C:\Users\foo\project, 盘符开头且不含路径非法字符
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])(?:[A-Za-z]:\\(?:[^\s<>:\"|?*\r\n]+\\)*[^\s<>:\"|?*\r\n]*)")
# POSIX 绝对路径: /home/foo/project, 至少两级目录, 避免误伤 /usr 这类短路径
_POSIX_PATH_RE = re.compile(r"(?<![\w])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")


def redact_text(text: str, options: RedactionOptions | None = None) -> str:
    """按选项脱敏普通文本。"""

    resolved = options or RedactionOptions()
    result = text
    # 先 secret 后路径: secret 值里可能混着路径片段, 先摘掉密钥再处理路径, 避免半截替换
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
