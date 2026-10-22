"""Regression tests for server, config, store and inbox fixes (one test per bug)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import importlib.util
import logging
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest
import uvicorn
import zstandard
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from jezero import server as server_mod
from jezero.cli import main as cli_main
from jezero.config import Config, ConnectorConfig, load_config
from jezero.server import _transport_security, build_server, clamp_limit, later_items
from jezero.store import Store

from .conftest import make_item

ROOT = Path(__file__).resolve().parents[1]
DEVICE_TOKEN = "device-token-for-fix-tests"
OTHER_TOKEN = "other-token-for-fix-tests"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


@contextmanager
def serve(config: Config, store: Store, host: str = "127.0.0.1") -> Iterator[str]:
    """Run the real app on a free port; yields the base URL (no /mcp)."""
    _, app = build_server(config, store)
    port = config.server.port
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def base_config(tmp_path: Path, **extra) -> dict:
    return {
        "storage": {"path": str(tmp_path / "lib.db")},
        "server": {"host": "127.0.0.1", "port": _free_port()},
        "clients": [
            {
                "name": "laptop",
                "token_sha256": sha(DEVICE_TOKEN),
                "sources": ["agent-*"],
                "upload": True,
            },
            {"name": "nobody", "token_sha256": sha(OTHER_TOKEN), "sources": ["nothing-*"]},
        ],
        "inbox": {"dir": str(tmp_path / "inbox"), "max_mb": 0.01},
        **extra,
    }


def new_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "lib.db")
    store.init()
    return store


async def _call_raw(url: str, token: str | None, tool: str, args: dict):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=10) as http,
        Client(streamable_http_client(url, http_client=http), cache=None) as client,
    ):
        return await client.call_tool(tool, args)


def call(url: str, token: str | None, tool: str, args: dict | None = None) -> dict:
    result = asyncio.run(_call_raw(url, token, tool, args or {}))
    assert not result.is_error, result
    assert result.structured_content is not None
    return result.structured_content


# -- 1. trust comes from the connector type ---------------------------------------------


def test_connector_trust_defaults_to_the_type_default() -> None:
    config = Config.model_validate(
        {
            "connectors": [
                {"name": "mail", "type": "gmail"},
                {"name": "mail-mine", "type": "gmail", "trust": "own"},
                {"name": "agent-codex", "type": "codex"},
            ]
        }
    )
    trust = {c.name: c.trust for c in config.connectors}
    assert trust == {"mail": "external", "mail-mine": "own", "agent-codex": "own"}
    assert all(c.effective_trust == c.trust for c in config.connectors)
    built = ConnectorConfig.model_construct(name="x", type="gmail", options={}, project=None)
    assert built.trust is None and built.effective_trust == "external"


# -- 2. import-only types are refused at load ---------------------------------------------


def test_import_only_connector_type_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match=r"jsonl is import-only: use `jezero import-jsonl`"):
        Config.model_validate({"connectors": [{"name": "old", "type": "jsonl"}]})


# -- 3. one key, one client ---------------------------------------------------------------


def test_duplicate_client_keys_are_refused() -> None:
    digest = sha("same")
    with pytest.raises(ValidationError, match="share the same token_sha256"):
        Config.model_validate(
            {
                "clients": [
                    {"name": "a", "token_sha256": digest},
                    {"name": "b", "token_sha256": digest},
                ]
            }
        )
    with pytest.raises(ValidationError, match="share the same token_env"):
        Config.model_validate(
            {"clients": [{"name": "a", "token_env": "K"}, {"name": "b", "token_env": "K"}]}
        )


def test_two_env_tokens_with_one_value_disable_both(monkeypatch) -> None:
    monkeypatch.setenv("FIX_KEY_A", "same-value")
    monkeypatch.setenv("FIX_KEY_B", "same-value")
    config = Config.model_validate(
        {
            "clients": [
                {"name": "a", "token_env": "FIX_KEY_A"},
                {"name": "b", "token_env": "FIX_KEY_B"},
            ]
        }
    )
    verifier = server_mod.StaticTokenVerifier(config.clients)
    assert verifier.active_clients == []
    assert asyncio.run(verifier.verify_token("same-value")) is None


# -- 4. later: same source without a project; configured candidate pool ------------------


def test_later_without_project_stays_in_the_items_source(tmp_path) -> None:
    store = new_store(tmp_path)
    store.upsert(
        make_item(
            "a1", "Backup plan for the router", "x", source="agent-a", project=None, days_ago=3
        )
    )
    store.upsert(
        make_item(
            "a2", "Backup plan for the router v2", "y", source="agent-a", project=None, days_ago=1
        )
    )
    store.upsert(
        make_item(
            "b1", "Backup plan for the router", "z", source="agent-b", project=None, days_ago=1
        )
    )
    store.upsert(make_item("p1", "Backup plan for the router", "w", source="agent-b", days_ago=1))
    first = next(i for i in store.timeline(limit=10) if i.external_id == "a1")
    newer, how = later_items(store, first)
    assert ([i.external_id for i in newer], how) == (["a2"], "subject")
    # Nothing on the subject: the fallback timeline stays in the source too.
    newer, how = later_items(store, first, query="unrelated words")
    assert ([i.external_id for i in newer], how) == (["a2"], "project_timeline")


def test_later_passes_the_configured_candidate_pool(tmp_path, monkeypatch) -> None:
    store = new_store(tmp_path)
    store.upsert(make_item("a1", "Pool test", "x", source="agent-a", days_ago=2))
    seen: list[int] = []
    original = Store.search

    def spy(self, query, **kwargs):
        seen.append(kwargs.get("candidate_pool"))
        return original(self, query, **kwargs)

    monkeypatch.setattr(Store, "search", spy)
    later_items(store, store.timeline(limit=1)[0], candidate_pool=37)
    assert seen == [37]
    config = Config.model_validate(base_config(tmp_path, search={"candidate_pool": 41}))
    item_id = store.timeline(limit=1)[0].id
    seen.clear()
    with serve(config, store) as base:
        call(f"{base}/mcp", DEVICE_TOKEN, "later", {"item_id": item_id})
    assert seen == [41]


# -- 5. one limit rule for every tool ------------------------------------------------------


def test_clamp_limit_rule() -> None:
    assert clamp_limit(None, 20, 50) == 20
    assert clamp_limit(0, 20, 50) == 20
    assert clamp_limit(-3, 5, 50) == 5
    assert clamp_limit(7, 20, 50) == 7
    assert clamp_limit(500, 20, 50) == 50
    assert clamp_limit(None, 20, 10) == 10


def test_tools_share_the_limit_rule(tmp_path) -> None:
    store = new_store(tmp_path)
    for n in range(30):
        store.upsert(
            make_item(
                f"s{n}", f"Limit topic number {n}", "limit topic", source="agent-a", days_ago=30 - n
            )
        )
    config = Config.model_validate(
        base_config(tmp_path, search={"default_limit": 3, "max_limit": 25})
    )
    first = min(store.timeline(limit=50), key=lambda i: i.occurred_at)
    with serve(config, store) as base:
        url = f"{base}/mcp"
        for limit in (None, 0, -1):
            args = {} if limit is None else {"limit": limit}
            assert call(url, DEVICE_TOKEN, "search", {"query": "limit topic", **args})["count"] == 3
            assert call(url, DEVICE_TOKEN, "timeline", args)["count"] == 20
            assert call(url, DEVICE_TOKEN, "later", {"item_id": first.id, **args})["count"] == 5
        assert call(url, DEVICE_TOKEN, "timeline", {"limit": 100})["count"] == 25
        assert call(url, DEVICE_TOKEN, "timeline", {"limit": 2})["count"] == 2


# -- 6. a bad date reaches the agent with its reason -----------------------------------------


def test_invalid_since_message_reaches_the_agent(tmp_path) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))
    with serve(config, store) as base:
        for tool, args in (
            ("search", {"query": "x", "since": "last tuesday"}),
            ("timeline", {"until": "soon"}),
        ):
            result = asyncio.run(_call_raw(f"{base}/mcp", DEVICE_TOKEN, tool, args))
            assert result.is_error
            text = result.content[0].text
            field = "since" if "since" in args else "until"
            assert f"{field} must be an ISO 8601 date or datetime" in text
            assert repr(args[field]) in text


# -- 7. overview keeps its days when the key reads nothing ------------------------------------


def test_overview_returns_days_when_no_source_matches(tmp_path) -> None:
    store = new_store(tmp_path)
    store.upsert(make_item("a1", "Something", "x", source="agent-a"))
    data = store.overview(allowed_sources=["nothing-*"], days=5)
    assert len(data["days"]) == 5 and data["items"] == 0 and data["sources"] == {}
    config = Config.model_validate(base_config(tmp_path))
    with serve(config, store) as base:
        body = httpx2.get(
            f"{base}/api/v1/overview?days=4", headers={"Authorization": f"Bearer {OTHER_TOKEN}"}
        ).json()
    assert body["ok"] and body["items"] == 0 and len(body["days"]) == 4


# -- 8. every route checks the Host header -----------------------------------------------------


def test_non_mcp_routes_refuse_a_foreign_host(tmp_path) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    with serve(config, store) as base:
        for method, path in (
            ("GET", "/api/v1/overview"),
            ("GET", "/inbox/v1/ping"),
            ("GET", "/client/jezero-client.tar.gz"),
            ("PUT", "/inbox/v1/claude-code/projects/p/x.jsonl"),
        ):
            evil = httpx2.request(
                method, base + path, headers={**auth, "Host": "evil.example:8765"}, content=b"{}"
            )
            assert evil.status_code == 421, (path, evil.status_code)
            origin = httpx2.request(
                method,
                base + path,
                headers={**auth, "Origin": "http://evil.example"},
                content=b"{}",
            )
            assert origin.status_code == 403, path
        assert httpx2.get(base + "/inbox/v1/ping", headers=auth).status_code == 200


# -- 9. any loopback host gets explicit allowed hosts ----------------------------------------


def test_transport_security_is_explicit_for_every_loopback_host() -> None:
    for host, pattern in (
        ("127.0.0.2", "127.0.0.2:*"),
        ("::1", "[::1]:*"),
        ("localhost", "localhost:*"),
    ):
        settings = _transport_security(Config.model_validate({"server": {"host": host}}))
        assert settings is not None and settings.enable_dns_rebinding_protection
        assert pattern in settings.allowed_hosts
    proxied = _transport_security(
        Config.model_validate(
            {
                "server": {
                    "host": "127.0.0.2",
                    "public_url": "https://library.example.org/mcp",
                    "allowed_hosts": ["alias.example.org"],
                }
            }
        )
    )
    assert {"library.example.org", "alias.example.org", "127.0.0.2:*"} <= set(proxied.allowed_hosts)
    assert "https://library.example.org" in proxied.allowed_origins


def test_server_on_another_loopback_address_checks_hosts(tmp_path) -> None:
    host = "127.0.0.2"
    try:
        port = _free_port(host)
    except OSError:  # pragma: no cover - platforms without the whole 127/8 range
        pytest.skip("127.0.0.2 is not bindable here")
    store = new_store(tmp_path)
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "server": {"host": host, "port": port, "require_auth": False},
        }
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}
    with serve(config, store, host=host) as base:
        bad = httpx2.post(f"{base}/mcp", json=body, headers={**headers, "Host": "evil.example"})
        assert bad.status_code == 421
        assert (
            httpx2.get(f"{base}/api/v1/overview", headers={"Host": "evil.example"}).status_code
            == 421
        )
        assert httpx2.get(f"{base}/api/v1/overview").status_code == 200
        assert httpx2.post(f"{base}/mcp", json=body, headers=headers).status_code == 200


# -- 10/11. chunked uploads stop at the limit; zstd is accepted ---------------------------------


def test_read_stream_stops_at_the_limit_without_reading_the_rest() -> None:
    from jezero import inbox

    pulled = 0

    async def endless():
        nonlocal pulled
        while True:
            pulled += 1
            yield b"x" * 1000

    with pytest.raises(inbox.InboxError) as caught:
        asyncio.run(inbox.read_stream(endless(), max_bytes=10_000))
    assert caught.value.status == 413 and pulled == 11


def test_chunked_upload_is_cut_at_the_limit_and_zstd_is_accepted(tmp_path) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))  # max_mb 0.01: about 10 KB
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    target = tmp_path / "inbox" / "laptop" / "claude-code" / "projects" / "p" / "s.jsonl"

    def chunks(total: int, size: int = 1024) -> Iterator[bytes]:
        for _ in range(total // size):
            yield b"x" * size

    with serve(config, store) as base:
        url = f"{base}/inbox/v1/claude-code/projects/p/s.jsonl"
        small = httpx2.put(url, content=chunks(4096), headers=auth)
        assert small.status_code == 200 and target.stat().st_size == 4096
        big = httpx2.put(url, content=chunks(64 * 1024), headers=auth)
        assert big.status_code == 413
        assert target.stat().st_size == 4096  # the refused upload changed nothing

        packed = zstandard.ZstdCompressor().compress(b'{"type":"user"}\n')
        ok = httpx2.put(url, content=packed, headers={**auth, "Content-Encoding": "zstd"})
        assert ok.status_code == 200 and target.read_bytes() == b'{"type":"user"}\n'
        bomb = zstandard.ZstdCompressor().compress(b"0" * 200_000)
        assert len(bomb) < 10_000
        zbomb = httpx2.put(url, content=bomb, headers={**auth, "Content-Encoding": "zstd"})
        assert zbomb.status_code == 413
        junk = httpx2.put(url, content=b"not zstd", headers={**auth, "Content-Encoding": "zstd"})
        assert junk.status_code == 400 and "zstd" in junk.json()["error"]
        gz = httpx2.put(
            url, content=gzip.compress(b"g\n"), headers={**auth, "Content-Encoding": "gzip"}
        )
        assert gz.status_code == 200 and target.read_bytes() == b"g\n"
        br = httpx2.put(url, content=b"x", headers={**auth, "Content-Encoding": "br"})
        assert br.status_code == 415


# -- 12. check-config fails when an enabled connector cannot run ----------------------------


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "jezero.toml"
    path.write_text('[storage]\npath = "lib.db"\n' + text, encoding="utf-8")
    return path


ENABLED_CODEX = """
[server]
require_auth = false

[[connectors]]
name = "agent-codex"
type = "codex"
[connectors.options]
sessions_dir = "sessions"
"""


def test_check_config_exit_codes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("JEZERO_SUMMARIZER_API_KEY", raising=False)
    monkeypatch.delenv("FIX_MISSING_TOKEN", raising=False)
    tutorial = write_config(tmp_path, "[server]\nrequire_auth = false\n")
    assert cli_main(["-c", str(tutorial), "check-config"]) == 0
    assert "problems:" not in capsys.readouterr().out

    cfg = write_config(tmp_path, ENABLED_CODEX)
    assert cli_main(["-c", str(cfg), "check-config"]) == 1
    out = capsys.readouterr().out
    assert "problems:" in out and "summarizer.model is not set" in out
    assert "JEZERO_SUMMARIZER_API_KEY (summarizer.api_key_env) is not set" in out

    monkeypatch.setenv("JEZERO_SUMMARIZER_API_KEY", "set-for-test")
    cfg = write_config(tmp_path, ENABLED_CODEX + '\n[summarizer]\nmodel = "small"\n')
    assert cli_main(["-c", str(cfg), "check-config"]) == 0
    assert "problems:" not in capsys.readouterr().out

    cfg = write_config(tmp_path, '[[clients]]\nname = "agent"\ntoken_env = "FIX_MISSING_TOKEN"\n')
    assert cli_main(["-c", str(cfg), "check-config"]) == 1
    assert "client agent: FIX_MISSING_TOKEN is not set" in capsys.readouterr().out


# -- 13. redact-stored uses the configured tokenizer -----------------------------------------


def test_redact_stored_uses_the_configured_tokenizer(tmp_path, capsys) -> None:
    cfg = write_config(tmp_path, "")
    cfg.write_text(
        '[storage]\npath = "lib.db"\ntokenizer = "unicode61 remove_diacritics 2"\n',
        encoding="utf-8",
    )
    assert cli_main(["-c", str(cfg), "init"]) == 0
    assert cli_main(["-c", str(cfg), "redact-stored", "--dry-run"]) == 0
    assert "would redact 0 secrets in 0 items" in capsys.readouterr().out


# -- 14. content_hash docstring ----------------------------------------------------------------


def test_content_hash_is_documented_as_the_redacted_text() -> None:
    source = (ROOT / "src" / "jezero" / "models.py").read_text(encoding="utf-8")
    assert "Hash of the raw source content" not in source
    assert "Hash of the redacted source text" in source


# -- 15. privacy check knows the 172.16/12 private range -----------------------------------------


def test_privacy_check_flags_172_16_slash_12_only() -> None:
    spec = importlib.util.spec_from_file_location(
        "privacy_check", ROOT / "scripts" / "privacy_check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def flagged(text: str) -> bool:
        return any(p.search(text) for label, p in module.BUILTIN if label == "private LAN address")

    def quad(*parts: int) -> str:  # built at run time so this file passes the check itself
        return ".".join(str(p) for p in parts)

    assert flagged(f"host {quad(172, 16, 4, 2)} up")
    assert flagged(f"at {quad(172, 31, 255, 1)}.")
    assert not flagged(f"public {quad(172, 32, 0, 1)}")
    assert not flagged(f"public {quad(172, 15, 0, 1)}")
    assert not flagged(f"version {quad(1, 172, 16, 0, 4)}")
    assert not flagged(f"oid {quad(172, 16, 0, 1, 5)}")
    assert not flagged(f"short {quad(172, 16, 4)}")


# -- 16. the bundle route says how to build it --------------------------------------------------


def test_missing_client_bundle_explains_how_to_build_it(tmp_path, caplog) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(
        base_config(
            tmp_path, server={"port": _free_port(), "client_bundle": str(tmp_path / "none.tar.gz")}
        )
    )
    with caplog.at_level(logging.WARNING, logger="jezero.server"), serve(config, store) as base:
        response = httpx2.get(f"{base}/client/jezero-client.tar.gz")
    assert response.status_code == 404
    assert "python3 scripts/build_clients.py" in response.json()["error"]
    warnings = [r for r in caplog.records if "client bundle not found" in r.getMessage()]
    assert len(warnings) == 1


# -- 17. clients reload when the config file changes --------------------------------------------


def test_clients_reload_live_from_the_config_file(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(server_mod, "RELOAD_INTERVAL_SECONDS", 0.0)
    port = _free_port()
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        f'[storage]\npath = "lib.db"\n\n[server]\nport = {port}\n\n'
        f'[[clients]]\nname = "admin"\ntoken_sha256 = "{sha(OTHER_TOKEN)}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    store = Store(config.db_path)
    store.init()
    store.upsert(make_item("a1", "Reload topic", "reload", source="agent-a"))
    device_auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    new_key = "rotated-device-token-for-tests"
    with serve(config, store) as base:
        assert httpx2.get(f"{base}/inbox/v1/ping", headers=device_auth).status_code == 401
        assert cli_main(["-c", str(cfg), "device", "approve", "laptop", sha(DEVICE_TOKEN)]) == 0
        assert httpx2.get(f"{base}/inbox/v1/ping", headers=device_auth).json()["device"] == "laptop"
        assert call(f"{base}/mcp", DEVICE_TOKEN, "search", {"query": "reload"})["count"] == 1

        assert cli_main(["-c", str(cfg), "device", "rotate", "laptop", sha(new_key)]) == 0
        assert httpx2.get(f"{base}/inbox/v1/ping", headers=device_auth).status_code == 401
        new_auth = {"Authorization": f"Bearer {new_key}"}
        assert httpx2.get(f"{base}/inbox/v1/ping", headers=new_auth).status_code == 200
        assert call(f"{base}/mcp", new_key, "list_sources")["sources"]

        # A broken edit keeps the previous clients, and says so in the log.
        good = cfg.read_text(encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="jezero.server"):
            cfg.write_text(good + "\n[[clients]]\nname = \n", encoding="utf-8")
            assert httpx2.get(f"{base}/inbox/v1/ping", headers=new_auth).status_code == 200
        assert any("keeping the previous" in r.getMessage() for r in caplog.records)
        cfg.write_text(good, encoding="utf-8")

        assert cli_main(["-c", str(cfg), "device", "revoke", "laptop"]) == 0
        assert httpx2.get(f"{base}/inbox/v1/ping", headers=new_auth).status_code == 401
        mcp = httpx2.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**new_auth, "Accept": "application/json, text/event-stream"},
        )
        assert mcp.status_code == 401
        assert call(f"{base}/mcp", OTHER_TOKEN, "search", {"query": "reload"})["count"] == 1


def test_device_approve_says_no_restart_is_needed(tmp_path, capsys) -> None:
    cfg = write_config(tmp_path, "")
    assert cli_main(["-c", str(cfg), "device", "approve", "pc", sha("k")]) == 0
    out = capsys.readouterr().out
    assert "the server picks up the new key within a second" in out
    assert "restart" not in out


# -- store helper used by the pipeline ---------------------------------------------------------


def test_prune_segments_keeps_current_segments_and_other_sessions(store: Store) -> None:
    for ext in ("s1#0", "s1#1", "s1#2", "s10#0", "s2#0"):
        store.upsert(make_item(ext, f"Segment {ext}", "segment text", source="agent-a"))
    store.upsert(make_item("s1#2", "Other source", "segment text", source="agent-b"))
    assert store.prune_segments("agent-a", "s1", ["s1#0", "s1#1"]) == 1
    left = sorted((i.source, i.external_id) for i in store.timeline(limit=20))
    assert left == [
        ("agent-a", "s1#0"),
        ("agent-a", "s1#1"),
        ("agent-a", "s10#0"),
        ("agent-a", "s2#0"),
        ("agent-b", "s1#2"),
    ]
    assert not [
        h
        for h in store.search("Segment")
        if h.item.external_id == "s1#2" and h.item.source == "agent-a"
    ]
