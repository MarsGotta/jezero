from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from biblioteca.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid() -> None:
    config = load_config(ROOT / "biblioteca.example.toml")
    assert config.server.require_auth
    assert {c.name for c in config.connectors} >= {"agent-hermes", "mail-personal"}
    assert config.db_path.is_absolute()


def test_inline_secret_in_options_is_rejected() -> None:
    with pytest.raises(ValidationError, match="looks like a secret"):
        Config.model_validate(
            {"connectors": [{"name": "x", "type": "gmail", "options": {"api_key": "abc"}}]}
        )


def test_env_reference_and_token_counts_are_allowed() -> None:
    Config.model_validate(
        {
            "connectors": [
                {"name": "x", "type": "gmail", "options": {"api_key_env": "X", "max_tokens": 10}}
            ]
        }
    )


def test_client_needs_exactly_one_token_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        Config.model_validate({"clients": [{"name": "a"}]})
    with pytest.raises(ValidationError, match="exactly one"):
        Config.model_validate(
            {"clients": [{"name": "a", "token_env": "X", "token_sha256": "0" * 64}]}
        )


def test_unknown_keys_and_types_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"storage": {"pth": "x.db"}})
    with pytest.raises(ValidationError, match="unknown connector types"):
        Config.model_validate({"connectors": [{"name": "x", "type": "nope"}]})


def test_no_auth_only_on_loopback() -> None:
    Config.model_validate({"server": {"host": "127.0.0.1", "require_auth": False}})
    with pytest.raises(ValidationError, match="loopback"):
        Config.model_validate({"server": {"host": "0.0.0.0", "require_auth": False}})


def test_tokenizer_is_sanitised() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"storage": {"tokenizer": "trigram'); DROP TABLE items; --"}})


def test_env_report_never_contains_values(monkeypatch) -> None:
    monkeypatch.setenv("BIBLIOTECA_TOKEN_CODING_AGENT", "super-secret-value")
    config = load_config(ROOT / "biblioteca.example.toml")
    report = config.env_report()
    assert report["BIBLIOTECA_TOKEN_CODING_AGENT"] is True
    assert "super-secret-value" not in repr(report)


def test_dotenv_next_to_config_is_loaded_without_overriding(tmp_path, monkeypatch) -> None:
    from biblioteca.config import load_env_file

    monkeypatch.delenv("BIB_TEST_A", raising=False)
    monkeypatch.setenv("BIB_TEST_B", "from-environment")
    env = tmp_path / ".env"
    env.write_text('# comment\nBIB_TEST_A="quoted value"\nexport BIB_TEST_B=from-file\nEMPTY=\n')
    assert load_env_file(env) == ["BIB_TEST_A"]
    import os

    assert os.environ["BIB_TEST_A"] == "quoted value"
    assert os.environ["BIB_TEST_B"] == "from-environment"
    monkeypatch.delenv("BIB_TEST_A")
