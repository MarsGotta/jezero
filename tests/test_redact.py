from __future__ import annotations

import pytest

from jezero.redact import Redactor

# Synthetic, obviously fake credentials built at runtime so no scanner flags this file.
FAKE_GH = "ghp_" + "a1B2c3D4e5" * 4
FAKE_ANT = "sk-ant-" + "x9" * 20
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.mark.parametrize(
    "secret",
    [FAKE_GH, FAKE_ANT, FAKE_AWS, "Bearer " + "t0ken" * 6],
)
def test_builtin_patterns(secret: str) -> None:
    out = Redactor().redact(f"before {secret} after")
    assert secret not in out.text
    assert out.count >= 1
    assert out.text.startswith("before ") and out.text.endswith(" after")


def test_assignment_keeps_key() -> None:
    out = Redactor().redact("password = hunter2-but-longer and more")
    assert "hunter2" not in out.text
    assert "password = [REDACTED:assignment]" in out.text


def test_url_credentials() -> None:
    out = Redactor().redact("postgres://user:pa55word@db.internal:5432/app")
    assert "pa55word" not in out.text


def test_private_key_block() -> None:
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----"
    assert "AAAA" not in Redactor().redact(block).text


def test_custom_pattern_and_disabled() -> None:
    assert "[REDACTED:custom]" in Redactor([r"ACME-\d{4}"]).redact("id ACME-1234").text
    assert Redactor(enabled=False).redact(FAKE_GH).text == FAKE_GH


def test_plain_text_untouched() -> None:
    text = "We decided to keep the index in one SQLite file."
    assert Redactor().redact(text).text == text
