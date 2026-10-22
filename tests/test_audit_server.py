"""Regression tests for the server and config findings of the security audit."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from pathlib import Path

import httpx2
import pytest
import zstandard
from pydantic import ValidationError

from jezero import server as server_mod
from jezero.config import Config, ConfigEditError, ConfigEditor, client_blocks, load_config
from jezero.connectors import codex as codex_mod
from jezero.connectors.codex import CodexConnector, open_text
from jezero.server import ClientRegistry, _check_item_id, _parse_dt
from jezero.store import (
    MAX_QUERY_TERMS,
    MAX_TERM_CHARS,
    SQLITE_MAX_INT,
    ConnectorState,
    build_match_query,
)

from .conftest import make_item
from .test_codex import NOW as CODEX_NOW
from .test_codex import make_tree
from .test_server_fixes import (
    DEVICE_TOKEN,
    OTHER_TOKEN,
    _call_raw,
    base_config,
    call,
    new_store,
    serve,
    sha,
)

ZST_PATH = "/inbox/v1/codex/sessions/2026/10/21/rollout-2026-10-21T10-00-00-x.jsonl.zst"


def _env_cleanup(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


# -- M1: no open library behind a proxy ---------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"public_url": "https://jezero.public.example/mcp"},
        {"allowed_hosts": ["jezero.public.example"]},
    ],
)
def test_require_auth_off_is_refused_with_public_url_or_allowed_hosts(extra) -> None:
    with pytest.raises(ValidationError, match="cannot be combined with public_url"):
        Config.model_validate({"server": {"host": "127.0.0.1", "require_auth": False, **extra}})
    # Still fine with a key, and still fine without one on a plain loopback server.
    Config.model_validate({"server": {"host": "127.0.0.1", "require_auth": True, **extra}})
    Config.model_validate({"server": {"host": "127.0.0.1", "require_auth": False}})


# -- M2: compressed uploads and bounded rollout reads ---------------------------------------


def test_zst_upload_is_measured_decompressed(tmp_path) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))  # max_mb = 0.01 (10 KiB)
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    bomb = zstandard.ZstdCompressor().compress(b"a" * 200_000)
    assert len(bomb) < 1024
    small = zstandard.ZstdCompressor().compress(b'{"type":"session_meta"}\n')
    target = tmp_path / "inbox" / "laptop" / "codex" / ZST_PATH.split("/codex/", 1)[1]
    with serve(config, store) as base:
        r = httpx2.put(base + ZST_PATH, content=bomb, headers=auth)
        assert r.status_code == 413 and r.json()["error"] == "file too large"
        assert not target.exists()
        # The same file inside a gzip transfer encoding is checked the same way.
        wrapped = {**auth, "Content-Encoding": "gzip"}
        r = httpx2.put(base + ZST_PATH, content=gzip.compress(bomb), headers=wrapped)
        assert r.status_code == 413 and not target.exists()
        # A small one is stored as sent, still compressed.
        r = httpx2.put(base + ZST_PATH, content=small, headers=auth)
        assert r.status_code == 200 and r.json()["bytes"] == len(small)
        assert target.read_bytes() == small


def _write_zst(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(zstandard.ZstdCompressor().compress(b"".join(lines)))


def test_open_text_skips_an_over_long_line_and_keeps_numbering(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(codex_mod, "MAX_LINE_BYTES", 1000)
    path = tmp_path / "rollout-x.jsonl.zst"
    _write_zst(path, [b'{"a": 1}\n', b'{"b": "' + b"x" * 5000 + b'"}\n', b'{"c": 3}\n', b"tail"])
    with caplog.at_level(logging.WARNING), open_text(path) as handle:
        lines = list(handle)
    assert lines == ['{"a": 1}\n', "", '{"c": 3}\n', "tail"]
    assert any("skipped a line longer than 1000 bytes" in r.getMessage() for r in caplog.records)
    plain = tmp_path / "rollout-y.jsonl"
    plain.write_bytes(b'{"a": 1}\n' + b"y" * 5000)  # over-long last line without newline
    with open_text(plain) as handle:
        assert handle.readline() == '{"a": 1}\n'
        assert list(handle) == [""]


def test_open_text_stops_after_the_total_cap(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(codex_mod, "MAX_ROLLOUT_BYTES", 30)
    path = tmp_path / "rollout-x.jsonl.zst"
    _write_zst(path, [b'{"n": 1}\n', b'{"n": 2}\n', b'{"n": 3}\n', b'{"n": 4}\n'])
    with caplog.at_level(logging.WARNING), open_text(path) as handle:
        lines = list(handle)
    # 30 bytes hold three whole lines (27 bytes); the cut fourth one is dropped.
    assert lines == ['{"n": 1}\n', '{"n": 2}\n', '{"n": 3}\n']
    assert any("stopped reading after 30 bytes" in r.getMessage() for r in caplog.records)


def test_open_text_reads_a_huge_single_line_in_bounded_memory(tmp_path) -> None:
    """A tiny .zst holding one 64 MiB line (over MAX_LINE_BYTES) is skipped in chunks."""
    path = tmp_path / "rollout-bomb.jsonl.zst"
    compressor = zstandard.ZstdCompressor()
    with path.open("wb") as raw, compressor.stream_writer(raw) as out:
        for _ in range(64):
            out.write(b"z" * (1 << 20))
        out.write(b"\n" + b'{"after": true}\n')
    assert path.stat().st_size < 64 * 1024
    with open_text(path) as handle:
        assert list(handle) == ["", '{"after": true}\n']


def test_codex_connector_unchanged_by_a_skipped_line(tmp_path, monkeypatch) -> None:
    """An over-long line becomes a skipped line: docs and their ids stay as they were."""
    monkeypatch.setattr(codex_mod, "MAX_LINE_BYTES", 4096)
    opts = {"min_chars": 0}

    def collect(filler: bytes) -> list[tuple[str, str]]:
        root = tmp_path / filler[:8].hex()
        sessions = make_tree(root)
        for path in sessions.rglob("*.jsonl"):
            stat = path.stat()
            head, _, rest = path.read_bytes().partition(b"\n")
            path.write_bytes(head + b"\n" + filler + b"\n" + rest)
            os.utime(path, (stat.st_atime, stat.st_mtime))
        conn = CodexConnector(
            "c", {**opts, "sessions_dir": str(sessions)}, max_segment_chars=20_000
        )
        docs = conn.collect(ConnectorState("c"), now=CODEX_NOW).docs
        return [(d.external_id, d.text) for d in docs]

    short = json.dumps({"type": "event_msg", "payload": {"type": "x"}}).encode()
    long = json.dumps({"type": "event_msg", "payload": {"blob": "q" * 10_000}}).encode()
    expected = collect(short)
    assert expected
    assert collect(long) == expected


# -- L1: token_env keys follow the .env file ------------------------------------------------


def _write_env_config(tmp_path: Path, name: str) -> Path:
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        f'[storage]\npath = "lib.db"\n\n[[clients]]\nname = "envclient"\ntoken_env = "{name}"\n',
        encoding="utf-8",
    )
    return cfg


def _bump(path: Path) -> None:
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))


def test_token_env_change_in_dotenv_applies_without_restart(tmp_path, monkeypatch) -> None:
    name = "AUDIT_FIX_TOKEN_A"
    _env_cleanup(monkeypatch, name)
    monkeypatch.setattr(server_mod, "RELOAD_INTERVAL_SECONDS", 0.0)
    cfg = _write_env_config(tmp_path, name)
    env = tmp_path / ".env"
    env.write_text(f"{name}=first-key-value\n", encoding="utf-8")
    try:
        registry = ClientRegistry(load_config(cfg))
        assert registry.authenticate("first-key-value") is not None

        env.write_text(f"{name}=second-key-value\n", encoding="utf-8")
        _bump(env)
        assert registry.authenticate("first-key-value") is None
        assert registry.authenticate("second-key-value") is not None

        env.write_text("# key removed\n", encoding="utf-8")
        _bump(env)
        assert registry.authenticate("second-key-value") is None
        assert name not in os.environ

        env.write_text(f"{name}=third-key-value\n", encoding="utf-8")
        _bump(env)
        assert registry.authenticate("third-key-value") is not None
        env.unlink()
        assert registry.authenticate("third-key-value") is None
    finally:
        os.environ.pop(name, None)


def test_service_manager_variable_still_wins_over_dotenv(tmp_path, monkeypatch) -> None:
    name = "AUDIT_FIX_TOKEN_B"
    monkeypatch.setenv(name, "from-the-service-manager")
    monkeypatch.setattr(server_mod, "RELOAD_INTERVAL_SECONDS", 0.0)
    cfg = _write_env_config(tmp_path, name)
    env = tmp_path / ".env"
    env.write_text(f"{name}=from-the-file\n", encoding="utf-8")
    registry = ClientRegistry(load_config(cfg))
    assert registry.authenticate("from-the-service-manager") is not None
    env.write_text(f"{name}=changed-in-the-file\n", encoding="utf-8")
    _bump(env)
    assert registry.authenticate("from-the-service-manager") is not None
    assert registry.authenticate("changed-in-the-file") is None
    env.write_text("", encoding="utf-8")
    _bump(env)
    assert os.environ[name] == "from-the-service-manager"


# -- L2: bounded query cost -----------------------------------------------------------------


def test_query_terms_are_capped(store) -> None:
    store.upsert(make_item("a", "Retry policy", "uploader retry policy"))
    huge = " ".join(f"term{i:06d}" for i in range(160_000))
    match = build_match_query(huge)
    assert match is not None and match.count(" OR ") == MAX_QUERY_TERMS - 1
    long_term = build_match_query("x" * 10_000)
    assert long_term == '"' + "x" * MAX_TERM_CHARS + '"'
    started = time.perf_counter()
    assert store.search(huge + " retry") == []  # "retry" is past the 4 KB cut
    assert time.perf_counter() - started < 2.0
    assert [h.item.external_id for h in store.search("retry " + huge)] == ["a"]


# -- L3: a broken config fails closed for removed or edited clients --------------------------


def test_broken_config_refuses_removed_and_edited_clients(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(server_mod, "RELOAD_INTERVAL_SECONDS", 0.0)
    third = "third-token-for-audit-tests"

    def client(name: str, token: str, sources: str = '["*"]') -> str:
        return (
            f'\n[[clients]]\nname = "{name}"\ntoken_sha256 = "{sha(token)}"\nsources = {sources}\n'
        )

    head = '[storage]\npath = "lib.db"\n'
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        head + client("keep", DEVICE_TOKEN) + client("gone", OTHER_TOKEN) + client("edit", third),
        encoding="utf-8",
    )
    registry = ClientRegistry(load_config(cfg))
    assert registry.active_clients == ["keep", "gone", "edit"]

    # "gone" removed, "edit" narrowed, and a typo that stops the file from loading.
    broken = head + "# a comment\n" + client("keep", DEVICE_TOKEN)
    broken += client("edit", third, '["agent-*"]') + "\n[server]\nport = \n"
    with caplog.at_level(logging.ERROR, logger="jezero.server"):
        cfg.write_text(broken, encoding="utf-8")
        _bump(cfg)
        assert registry.authenticate(DEVICE_TOKEN) is not None
        assert registry.authenticate(OTHER_TOKEN) is None
        assert registry.authenticate(third) is None
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "could not be loaded" in message and "(edit, gone)" in message

    # Once it loads again, the file is the authority.
    cfg.write_text(head + client("keep", DEVICE_TOKEN) + client("edit", third), encoding="utf-8")
    _bump(cfg)
    assert registry.active_clients == ["keep", "edit"]
    assert registry.authenticate(OTHER_TOKEN) is None


def test_client_blocks_scan_without_toml() -> None:
    text = (
        '[[clients]]\nname = "a"\ntoken_env = "A"\n# trailing comment\n\n'
        "[[clients]]\nname = \n"
        "[[clients]]\nname = 'b'\nsources = [\"x\"]\n"
        '[[clients]]\nname = "c"\n[[clients]]\nname = "c"\n'
    )
    assert client_blocks(text) == {
        "a": 'name = "a"\ntoken_env = "A"',
        "b": "name = 'b'\nsources = [\"x\"]",
    }


# -- I1: Host and Origin before the key, on every route ------------------------------------


def test_host_and_origin_are_checked_before_the_key_everywhere(tmp_path) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    with serve(config, store) as base:
        no_key = httpx2.post(base + "/mcp", json=body, headers=accept)
        assert no_key.status_code == 401  # good host, no key
        for method, path in (
            ("POST", "/mcp"),
            ("GET", "/.well-known/oauth-protected-resource/mcp"),
            ("GET", "/api/v1/overview"),
            ("GET", "/inbox/v1/ping"),
            ("GET", "/nowhere"),
        ):
            evil = httpx2.request(
                method, base + path, json=body, headers={**accept, "Host": "evil.example"}
            )
            assert evil.status_code == 421, (path, evil.status_code)
            assert evil.text == "Invalid Host header"
            origin = httpx2.request(
                method, base + path, json=body, headers={**accept, "Origin": "http://evil.example"}
            )
            assert origin.status_code == 403, (path, origin.status_code)
        metadata = httpx2.get(base + "/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200


# -- I2: hostile arguments get clear tool errors -------------------------------------------


def test_odd_arguments_get_clear_errors_not_crashes(tmp_path) -> None:
    store = new_store(tmp_path)
    store.upsert(make_item("a1", "Retry policy", "uploader", source="agent-a"))
    config = Config.model_validate(base_config(tmp_path))
    with serve(config, store) as base:
        url = base + "/mcp"
        assert call(url, DEVICE_TOKEN, "search", {"query": "\x00\x00\x00"})["count"] == 0
        assert call(url, DEVICE_TOKEN, "search", {"query": "retry\x00policy\x1f"})["count"] == 1
        assert call(url, DEVICE_TOKEN, "get_item", {"item_id": 0}) == {"found": False}
        for tool, args, expected in (
            ("get_item", {"item_id": 2**70}, "item_id is out of range"),
            ("later", {"item_id": -(2**70)}, "item_id is out of range"),
            ("search", {"query": "retry", "since": "9999-12-31T23:59:59-05:00"}, "out of range"),
            ("timeline", {"until": "0001-01-01T00:00:00+05:00"}, "out of range"),
        ):
            result = asyncio.run(_call_raw(url, DEVICE_TOKEN, tool, args))
            assert result.is_error, (tool, args)
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert expected in text, text


def test_item_id_and_date_helpers() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    assert _check_item_id(SQLITE_MAX_INT) == SQLITE_MAX_INT
    assert _check_item_id(0) == 0  # in range: answers found false, not an error
    for bad in (-SQLITE_MAX_INT - 2, SQLITE_MAX_INT + 1, 2**70):
        with pytest.raises(ToolError):
            _check_item_id(bad)
    parsed = _parse_dt("2026-01-31T09:00:00+02:00", "since")
    assert parsed is not None and parsed.isoformat() == "2026-01-31T07:00:00+00:00"


def test_store_get_out_of_range_is_none(store) -> None:
    assert store.get(2**70) is None
    assert store.get(-(2**70)) is None


# -- I3: per-device inbox quota ------------------------------------------------------------


def test_device_quota_refuses_with_507(tmp_path) -> None:
    store = new_store(tmp_path)
    raw = base_config(tmp_path)
    raw["inbox"] = {"dir": str(tmp_path / "inbox"), "max_mb": 1, "max_device_mb": 0.01}
    config = Config.model_validate(raw)
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    first = f"{{}}\n{'a' * 6000}\n".encode()
    with serve(config, store) as base:
        url = base + "/inbox/v1/claude-code/projects/p/{}.jsonl"
        assert httpx2.put(url.format("one"), content=first, headers=auth).status_code == 200
        # Replacing the same file does not count its old size.
        assert httpx2.put(url.format("one"), content=first, headers=auth).status_code == 200
        r = httpx2.put(url.format("two"), content=first, headers=auth)
        assert r.status_code == 507 and "max_device_mb" in r.json()["error"]
        assert not (tmp_path / "inbox" / "laptop" / "claude-code" / "projects/p/two.jsonl").exists()


def test_device_quota_is_off_by_default() -> None:
    assert Config.model_validate({}).inbox.max_device_mb is None
    with pytest.raises(ValidationError):
        Config.model_validate({"inbox": {"max_device_mb": 0}})


# -- ConfigEditor: no lost update ------------------------------------------------------------


def test_config_editor_refuses_a_lost_update(tmp_path) -> None:
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        '[storage]\npath = "lib.db"\n\n'
        f'[[clients]]\nname = "a"\ntoken_sha256 = "{sha("a-key")}"\n\n'
        f'[[clients]]\nname = "b"\ntoken_sha256 = "{sha("b-key")}"\n',
        encoding="utf-8",
    )
    first, second = ConfigEditor(cfg), ConfigEditor(cfg)
    block = first.find("clients", "a")
    assert block is not None
    first.set_value(block, "upload", "true")
    second.remove(second.find("clients", "b"))  # type: ignore[arg-type]
    first.commit()
    with pytest.raises(ConfigEditError, match="changed since it was read"):
        second.commit()
    config = load_config(cfg)
    assert [c.name for c in config.clients] == ["a", "b"]
    assert config.clients[0].upload is True
    assert not list(tmp_path.glob(".jezero-edit-*"))
    # Read again, the second edit goes through.
    again = ConfigEditor(cfg)
    again.remove(again.find("clients", "b"))  # type: ignore[arg-type]
    again.commit()
    assert [c.name for c in load_config(cfg).clients] == ["a"]
