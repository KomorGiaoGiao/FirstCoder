"""PII 脱敏规则测试。

合法身份证样本的校验位按 ISO 7064 MOD 11-2 规则算出:
11010519491231002X / 440301199001011234 / 310110199512128887。
"""

from pathlib import Path

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.models import PiiOptions, RedactionOptions, ShareOptions
from firstcoder.session.pii import redact_pii
from firstcoder.session.redaction import redact_text
from firstcoder.session.share import SessionShareService
from firstcoder.session.transcript import TranscriptBuilder


def test_redact_pii_masks_provider_api_keys() -> None:
    text = " ".join(
        [
            "sk-1234567890abcdefghij",
            "sk-ant-api03-abcdefghijklmnopqrst",
            "sk-proj-abcdefghijklmnopqrstuvwx",
            "gsk_abcdefghijklmnopqrst",
            "AIzaSyDabcdefghijklmnopqrstuvwxyz12345678",
            "xai-abcdefghijklmnopqrst",
            "hf_abcdefghijklmnopqrst",
        ]
    )

    redacted = redact_pii(text)

    assert redacted == " ".join(["[REDACTED_API_KEY]"] * 7)


def test_redact_pii_keeps_non_key_tokens() -> None:
    text = "sk-short sk-12345 task-1234567890abcdefghij hello"

    assert redact_pii(text) == text


def test_redact_pii_masks_mainland_phone_numbers() -> None:
    for phone in ("13912345678", "+8613912345678", "0086 13912345678", "+86-13912345678"):
        assert redact_pii(f"联系我 {phone} 吧") == "联系我 [REDACTED_PHONE] 吧"


def test_redact_pii_keeps_non_phone_numbers() -> None:
    # 12 号段无效 / 10 位不够 / 长数字串内部的 11 位子串都不算手机号
    text = "12912345678 1234567890 21391234567890"

    assert redact_pii(text) == text


def test_redact_pii_masks_valid_id_numbers() -> None:
    for id_number in ("11010519491231002X", "440301199001011234", "310110199512128887"):
        assert redact_pii(f"身份证号 {id_number}") == "身份证号 [REDACTED_ID]"


def test_redact_pii_keeps_invalid_id_candidates() -> None:
    # 依次: 校验位错 / 省份码不存在 / 13 月不存在 / 1899 年越界 / 15 位旧证不在覆盖内
    text = (
        "110105194912310021 99010519491231002X 11010519491331002X"
        " 11010518991231002X 110105491231002"
    )

    assert redact_pii(text) == text


def test_redact_pii_partial_strategy_keeps_head_and_tail() -> None:
    text = "手机 +86 13912345678 证件 11010519491231002X 密钥 sk-1234567890abcdefghij"

    redacted = redact_pii(text, PiiOptions(strategy="partial"))

    assert "+86 139****5678" in redacted
    assert "110105********002X" in redacted
    assert "sk-12345****ghij" in redacted


def test_redact_pii_off_strategy_returns_text_unchanged() -> None:
    text = "13912345678 11010519491231002X sk-1234567890abcdefghij"

    assert redact_pii(text, PiiOptions(strategy="off")) == text


def test_redact_text_combines_secret_path_and_pii_rules() -> None:
    text = "API_KEY=sk-1234567890abcdefghij 备用 sk-ant-api03-abcdefghijklmnopqrst 电话 13912345678"

    redacted = redact_text(text, RedactionOptions(pii=PiiOptions()))

    assert "API_KEY=[REDACTED_SECRET]" in redacted
    assert "sk-ant-api03-abcdefghijklmnopqrst" not in redacted
    assert "[REDACTED_API_KEY]" in redacted
    assert "[REDACTED_PHONE]" in redacted


def test_redact_text_without_pii_options_keeps_pii() -> None:
    text = "电话 13912345678"

    assert redact_text(text) == text


def test_transcript_builder_redacts_pii_when_options_set(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="pii")
    writer.append_user_message("我的手机 13912345678, 身份证 11010519491231002X")

    transcript = TranscriptBuilder(store).build("sess_test", ShareOptions(pii=PiiOptions()))

    assert transcript.entries[0].content == "我的手机 [REDACTED_PHONE], 身份证 [REDACTED_ID]"


def test_transcript_builder_keeps_pii_without_options(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="pii")
    writer.append_user_message("我的手机 13912345678")

    transcript = TranscriptBuilder(store).build("sess_test")

    assert "13912345678" in transcript.entries[0].content


def test_share_service_injects_configured_pii_options(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="pii")
    writer.append_user_message("密钥 sk-1234567890abcdefghij 手机 13912345678")

    path = SessionShareService(store, pii_options=PiiOptions()).export_markdown("sess_test")
    content = path.read_text(encoding="utf-8")

    assert "sk-1234567890abcdefghij" not in content
    assert "[REDACTED_API_KEY]" in content
    assert "[REDACTED_PHONE]" in content


def test_share_service_caller_options_override_configured_pii(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")
    writer.append_session_created(title="pii")
    writer.append_user_message("我的手机 13912345678")

    service = SessionShareService(store, pii_options=PiiOptions())
    path = service.export_markdown("sess_test", options=ShareOptions(pii=PiiOptions(strategy="off")))
    content = path.read_text(encoding="utf-8")

    assert "13912345678" in content
