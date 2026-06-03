from firstcoder.session.models import RedactionOptions
from firstcoder.session.redaction import redact_text


def test_redact_text_redacts_secret_assignments() -> None:
    text = "OPENAI_API_KEY=sk-test SECRET: abc PASSWORD = hunter2 COOKIE=session"

    redacted = redact_text(text)

    assert "sk-test" not in redacted
    assert "abc" not in redacted
    assert "hunter2" not in redacted
    assert "session" not in redacted
    assert "OPENAI_API_KEY=[REDACTED_SECRET]" in redacted
    assert "SECRET: [REDACTED_SECRET]" in redacted
    assert "PASSWORD = [REDACTED_SECRET]" in redacted
    assert "COOKIE=[REDACTED_SECRET]" in redacted


def test_redact_text_redacts_json_like_secret_values() -> None:
    text = '{"access_token": "tok_123", "normal": "value", "clientSecret": "secret_123"}'

    redacted = redact_text(text)

    assert "tok_123" not in redacted
    assert "secret_123" not in redacted
    assert '"access_token": "[REDACTED_SECRET]"' in redacted
    assert '"clientSecret": "[REDACTED_SECRET]"' in redacted
    assert '"normal": "value"' in redacted


def test_redact_text_redacts_windows_and_posix_paths() -> None:
    text = "read D:\\Komor_Code\\FirstCoder\\README.md and /home/user/project/secret.txt"

    redacted = redact_text(text)

    assert "D:\\Komor_Code" not in redacted
    assert "/home/user/project" not in redacted
    assert redacted.count("[REDACTED_PATH]") == 2


def test_redact_text_keeps_disabled_categories() -> None:
    text = "TOKEN=abc D:\\Project\\file.txt /tmp/demo.txt"

    redacted = redact_text(
        text,
        RedactionOptions(redact_paths=False, redact_secrets=False),
    )

    assert redacted == text


def test_redact_text_can_disable_only_paths_or_only_secrets() -> None:
    text = "TOKEN=abc D:\\Project\\file.txt"

    keep_paths = redact_text(text, RedactionOptions(redact_paths=False, redact_secrets=True))
    keep_secrets = redact_text(text, RedactionOptions(redact_paths=True, redact_secrets=False))

    assert "TOKEN=[REDACTED_SECRET]" in keep_paths
    assert "D:\\Project\\file.txt" in keep_paths
    assert "TOKEN=abc" in keep_secrets
    assert "[REDACTED_PATH]" in keep_secrets


def test_redact_text_handles_empty_text() -> None:
    assert redact_text("") == ""
