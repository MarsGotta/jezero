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


@pytest.mark.parametrize(
    "text",
    [
        "export GITHUB_TOKEN=abcd1234efgh5678ijkl",
        "DATABASE_PASSWORD=Sup3rS3cret!x",
        "export PGPASSWORD=examplepw",
        "CLIENT_SECRET: 9f8e7d6c5b4a3210zz",
        "TF_VAR_db_pass=abcdef123",
        '{"accessToken":"abc123def456ghi"}',
        '"apiKey": "not-a-real-key-123"',
        "--password hunter2222",
        "--token=abcdef123456",
        "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
        "glpat-AbCdEfGhIjKlMnOpQrSt",
        "AccountName=x;AccountKey=AbCd/EfGh+IjKl==;EndpointSuffix=core.windows.net",
        "https://x.example/c?sv=2022&sig=AbCdEf%2BGhIjKl%3D",
    ],
)
def test_more_secret_shapes(text: str) -> None:
    assert Redactor().redact(text).count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "TOKEN=$GITHUB_TOKEN",
        'token_env = "JEZERO_TOKEN"',
        "GITHUB_TOKEN_FILE=/run/secrets/x",
        "max_tokens=409600",
        "author: Jane Doe",
        "tokens: 128000 de contexto",
        "password = os.environ['DB']",
        "--token-file /tmp/tok.txt",
        "secret = ${SECRET_VALUE}",
        "3 tests PASS: 12 ok",
        "persist-credentials: false",
        "OAuth: configured",
        "pwd: /home/user/x",
        "kv_bytes_per_token = 2*1024",
        "(ALL) NOPASSWD: /usr/bin/systemctl",
        """data-cf-beacon='{"token": "<site-id>"}'""",
        "Missing bearer or basic authentication",
        "postgres://[REDACTED:url_credentials]@db/x",
        "token = get_access_token()",
        "if not token:\n    log.warning(x)",
        '"x-api-key": self._key,',
        "{ url: string; token: string }",
        "API_KEY=\nTOKEN_X=",
        "password: config.db_password",
    ],
)
def test_references_and_prose_are_kept(text: str) -> None:
    assert Redactor().redact(text).count == 0
