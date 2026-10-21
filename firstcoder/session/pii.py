"""PII 脱敏规则。

规则驱动、本地改写, 不调用外部 API; 作为中间件挂在导出链路上, 在敏感数据
"离开工作区之前"就地替换。覆盖三类模式:

- Provider API Key: sk- 系列 (含 sk-ant-/sk-proj-), gsk_, AIza, xai-, hf_
- 中国大陆手机号: 1[3-9] 开头 11 位, 可选 +86 / 0086 前缀
- 18 位身份证号: 省份码 + 出生日期 (1900-2099) + MOD 11-2 校验位三重验证

刻意收窄: 15 位旧身份证、其他国家号码不在覆盖内。
"""

from __future__ import annotations

import re
from datetime import date

from firstcoder.session.models import PiiOptions

PII_API_KEY_VALUE = "[REDACTED_API_KEY]"
PII_PHONE_VALUE = "[REDACTED_PHONE]"
PII_ID_VALUE = "[REDACTED_ID]"

# 各大模型厂商密钥格式; 长度下界放宽, 宁可多匹配也不让变体逃逸
_API_KEY_RE = re.compile(
    r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"
    r"|gsk_[A-Za-z0-9]{20,}"
    r"|AIza[0-9A-Za-z_-]{35,}"
    r"|xai-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,})\b"
)
# 前后边界断言: 防止命中更长数字串 (如订单号) 内部的 11 位子串
_PHONE_RE = re.compile(r"(?<![\d+])(?:\+86[\s-]?|0086[\s-]?)?1[3-9]\d{9}(?!\d)")
# partial 策略下拆出国家码前缀, 保留 "+86 " 让人认出这是带前缀的号码
_PHONE_PREFIX_RE = re.compile(r"(?P<prefix>(?:\+86|0086)[\s-]?)?(?P<number>1[3-9]\d{9})")
# 18 位候选粗筛, 是否真身份证由 _is_valid_id 逐候选验证
_ID_CANDIDATE_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

# GB/T 2260 省级行政区划码 (含港澳台 71/81/82)
_PROVINCE_CODES = frozenset(
    {
        "11", "12", "13", "14", "15",
        "21", "22", "23",
        "31", "32", "33", "34", "35", "36", "37",
        "41", "42", "43", "44", "45", "46",
        "50", "51", "52", "53", "54",
        "61", "62", "63", "64", "65",
        "71", "81", "82",
    }
)
# ISO 7064:1983 MOD 11-2 校验位权重与映射
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_MAP = "10X98765432"


def redact_pii(text: str, options: PiiOptions | None = None) -> str:
    """按策略改写文本中的 PII; off 或空文本直接返回原文。"""

    resolved = options or PiiOptions()
    if resolved.strategy == "off" or not text:
        return text
    # 先长后短: API key/身份证处理完再扫手机号, 避免手机号规则碰到残留数字位
    result = _API_KEY_RE.sub(lambda match: _mask_api_key(match.group(0), resolved.strategy), text)
    result = _ID_CANDIDATE_RE.sub(
        lambda match: _mask_id(match.group(0), resolved.strategy) if _is_valid_id(match.group(0)) else match.group(0),
        result,
    )
    return _PHONE_RE.sub(lambda match: _mask_phone(match.group(0), resolved.strategy), result)


def _is_valid_id(value: str) -> bool:
    """省份码 + 出生日期 + 校验位三重验证, 挡住随机 18 位数字。"""

    if value[:2] not in _PROVINCE_CODES:
        return False
    year = int(value[6:10])
    if not 1900 <= year <= 2099:
        return False
    try:
        date(year, int(value[10:12]), int(value[12:14]))
    except ValueError:
        return False
    expected = _ID_CHECK_MAP[sum(int(digit) * weight for digit, weight in zip(value[:17], _ID_WEIGHTS)) % 11]
    return expected == value[17].upper()


def _mask_api_key(value: str, strategy: str) -> str:
    if strategy == "partial" and len(value) > 16:
        return f"{value[:8]}****{value[-4:]}"
    return PII_API_KEY_VALUE


def _mask_phone(value: str, strategy: str) -> str:
    if strategy != "partial":
        return PII_PHONE_VALUE
    parsed = _PHONE_PREFIX_RE.fullmatch(value)
    if parsed is None:
        return PII_PHONE_VALUE
    number = parsed.group("number")
    return f"{parsed.group('prefix') or ''}{number[:3]}****{number[-4:]}"


def _mask_id(value: str, strategy: str) -> str:
    if strategy == "partial":
        # 保留省份+区县码和尾号, 遮住出生日期与顺序码
        return f"{value[:6]}********{value[-4:]}"
    return PII_ID_VALUE


__all__ = [
    "PII_API_KEY_VALUE",
    "PII_ID_VALUE",
    "PII_PHONE_VALUE",
    "redact_pii",
]
