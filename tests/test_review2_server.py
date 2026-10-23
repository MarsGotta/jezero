"""Second review: server, config, inbox, store and watchdog fixes (one test per bug)."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jezero import config as config_mod
from jezero import inbox
from jezero.config import (
    Config,
    SummarizerConfig,
    _edit_lock,
    format_config_error,
    load_config,
)
from jezero.server import INSTRUCTIONS, ClientRegistry, build_server
from jezero.store import MAX_QUERY_CHARS, ConnectorState, build_match_query
from jezero.watchdog import alert, evaluate, heartbeat

from .test_server_fixes import DEVICE_TOKEN, _call_raw, base_config, new_store, serve, sha


@pytest.fixture(autouse=True)
def _clean_env_ownership():
    yield
    config_mod._ENV_FROM_FILE.clear()
    inbox._USAGE.clear()


def _app(tmp_path, **extra):
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path, **extra))
    _, app = build_server(config, store)
    return app


def _token_env_config(tmp_path: Path, var: str) -> Path:
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        f'[[clients]]\nname = "laptop"\ntoken_env = "{var}"\n',
        encoding="utf-8",
    )
    return cfg


def _reload(registry: ClientRegistry):
    registry._checked = 0.0
    return registry.current()


# -- 1a. a re-pointed .env symlink is still the same file -------------------------------------


def test_env_symlink_swap_applies_the_rotated_value(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("R2_SWAP", raising=False)
    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    (v1 / "env").write_text("R2_SWAP=old-value-1\n")
    (v2 / "env").write_text("R2_SWAP=new-value-2\n")
    env = tmp_path / ".env"
    env.symlink_to(v1 / "env")
    try:
        config_mod.load_env_file(env)
        assert os.environ["R2_SWAP"] == "old-value-1"
        env.unlink()
        env.symlink_to(v2 / "env")  # rotation, as `ln -sfn` or a Kubernetes ..data swap
        config_mod.load_env_file(env)
        assert os.environ["R2_SWAP"] == "new-value-2"
    finally:
        monkeypatch.delenv("R2_SWAP", raising=False)


def test_registry_sees_a_symlink_swap_with_identical_stamps(tmp_path, monkeypatch) -> None:
    """The new target has the same size and mtime: the resolved path still differs."""
    monkeypatch.delenv("R2_SWAPREG", raising=False)
    cfg = _token_env_config(tmp_path, "R2_SWAPREG")
    data1, data2 = tmp_path / "..2026_01", tmp_path / "..2026_02"
    data1.mkdir()
    data2.mkdir()
    (data1 / "env").write_text("R2_SWAPREG=old-secret-aaaa\n")
    (data2 / "env").write_text("R2_SWAPREG=new-secret-bbbb\n")
    for path in (data1 / "env", data2 / "env"):
        os.utime(path, ns=(10**18, 10**18))
    link = tmp_path / "..data"
    link.symlink_to(data1)
    (tmp_path / ".env").symlink_to(Path("..data") / "env")
    try:
        registry = ClientRegistry(load_config(cfg))
        assert registry.authenticate("old-secret-aaaa") is not None
        tmp_link = tmp_path / "..data_tmp"
        tmp_link.symlink_to(data2)
        os.replace(tmp_link, link)  # the atomic swap Kubernetes does
        _reload(registry)
        assert registry.authenticate("new-secret-bbbb") is not None
        assert registry.authenticate("old-secret-aaaa") is None
    finally:
        monkeypatch.delenv("R2_SWAPREG", raising=False)


def test_config_folder_reached_through_a_symlink_keeps_one_env_owner(tmp_path, monkeypatch) -> None:
    """Reloads read the .env through the same (unresolved) path as the first load."""
    monkeypatch.delenv("R2_DIRLINK", raising=False)
    real = tmp_path / "real"
    real.mkdir()
    _token_env_config(real, "R2_DIRLINK")
    env = real / ".env"
    env.write_text("R2_DIRLINK=first-secret-1\n")
    (tmp_path / "etc").symlink_to(real)
    try:
        registry = ClientRegistry(load_config(tmp_path / "etc" / "jezero.toml"))
        env.write_text("R2_DIRLINK=second-secret-2\n")
        os.utime(env, ns=(1, 1))
        _reload(registry)
        assert os.environ["R2_DIRLINK"] == "second-secret-2"
        assert registry.authenticate("second-secret-2") is not None
        assert registry.authenticate("first-secret-1") is None
    finally:
        monkeypatch.delenv("R2_DIRLINK", raising=False)


# -- 1b. a .env with invalid UTF-8 still rotates; an unreadable one drops token_env keys --------


def test_invalid_utf8_env_still_applies_the_rotated_key(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.delenv("R2_KEY", raising=False)
    cfg = _token_env_config(tmp_path, "R2_KEY")
    env = tmp_path / ".env"
    env.write_text("R2_KEY=old-secret-value\n", encoding="utf-8")
    try:
        registry = ClientRegistry(load_config(cfg))
        assert registry.authenticate("old-secret-value") is not None
        env.write_bytes("# contrase\xf1a nueva\nR2_KEY=new-secret-value\n".encode("latin-1"))
        os.utime(env, ns=(1, 1))
        with caplog.at_level(logging.WARNING, logger="jezero.config"):
            _reload(registry)
        assert os.environ.get("R2_KEY") == "new-secret-value"
        assert registry.authenticate("new-secret-value") is not None
        assert registry.authenticate("old-secret-value") is None
        assert "not valid UTF-8" in caplog.text
        assert "new-secret-value" not in caplog.text
    finally:
        monkeypatch.delenv("R2_KEY", raising=False)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, not root"
)
def test_unreadable_env_drops_every_token_env_client(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("R2_UNREAD", raising=False)
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        '[[clients]]\nname = "laptop"\ntoken_env = "R2_UNREAD"\n\n'
        f'[[clients]]\nname = "phone"\ntoken_sha256 = "{sha(DEVICE_TOKEN)}"\n',
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("R2_UNREAD=readable-secret\n")
    try:
        registry = ClientRegistry(load_config(cfg))
        assert sorted(registry.active_clients) == ["laptop", "phone"]
        env.chmod(0)
        _reload(registry)
        assert registry.authenticate("readable-secret") is None
        assert registry.authenticate(DEVICE_TOKEN) is not None  # digest keys stay
        env.chmod(0o600)  # readable again: the client comes back
        _reload(registry)
        assert sorted(registry.active_clients) == ["laptop", "phone"]
    finally:
        env.chmod(0o600)
        monkeypatch.delenv("R2_UNREAD", raising=False)


def test_env_duplicate_key_last_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("R2_DUP", raising=False)
    env = tmp_path / ".env"
    env.write_text("R2_DUP=first\nR2_DUP=second\n")
    try:
        config_mod.load_env_file(env)
        assert os.environ["R2_DUP"] == "second"
    finally:
        monkeypatch.delenv("R2_DUP", raising=False)


# -- 2. websocket scopes do not crash the Host/Origin guard ----------------------------------


def test_websocket_scope_is_checked_not_crashed(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/mcp", headers={"host": "evil.example:8000"}),
    ):
        pass
    assert refused.value.code == 1008
    # An allowed host goes through to the router (no websocket route: closed, no crash).
    with (
        pytest.raises(WebSocketDisconnect) as passed,
        client.websocket_connect("/mcp", headers={"host": "127.0.0.1:8000"}),
    ):
        pass
    assert passed.value.code != 1008


# -- 3. host names are not case-sensitive; ports are ----------------------------------------


def test_host_check_ignores_the_case_of_the_host_name(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app, base_url="http://LOCALHOST:8000")
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    r = client.get("/inbox/v1/ping", headers={**auth, "host": "LOCALHOST:8000"})
    assert r.status_code == 200, r.text
    r = client.get(
        "/inbox/v1/ping",
        headers={**auth, "host": "LocalHost:8000", "origin": "HTTP://LOCALHOST:8000"},
    )
    assert r.status_code == 200, r.text
    r = client.get("/inbox/v1/ping", headers={**auth, "host": "Evil.Example:8000"})
    assert r.status_code == 421


def test_public_url_host_is_matched_in_any_case_with_its_exact_port(tmp_path) -> None:
    app = _app(tmp_path, server={"public_url": "https://Library.Example.org:8443/mcp"})
    client = TestClient(app, base_url="https://library.example.org:8443")
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    for host in ("library.example.org:8443", "LIBRARY.EXAMPLE.ORG:8443"):
        r = client.get("/inbox/v1/ping", headers={**auth, "host": host})
        assert r.status_code == 200, (host, r.text)
    # The IPv6 brackets stay, the host check still applies to them.
    r = client.get("/inbox/v1/ping", headers={**auth, "host": "[::1]:9"})
    assert r.status_code == 200
    r = client.get("/inbox/v1/ping", headers={**auth, "host": "[::2]:9"})
    assert r.status_code == 421


def test_mcp_route_accepts_an_uppercase_host(tmp_path) -> None:
    """The SDK's own check behind the guard sees the normalised Host too."""
    app = _app(tmp_path)
    with TestClient(app, base_url="http://LOCALHOST:8000") as client:
        r = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {DEVICE_TOKEN}",
                "host": "LOCALHOST:8000",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
    assert r.status_code == 200, r.text


# -- 4. unicode project names ----------------------------------------------------------------


def _put(client: TestClient, project: str | bytes | None, name: str = "a.jsonl"):
    headers = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    if project is not None:
        headers["X-Jezero-Project"] = project  # type: ignore[assignment]
    return client.put(
        f"/inbox/v1/claude-code/projects/p/{name}", content=b'{"x":1}\n', headers=headers
    )


def test_inbox_accepts_a_percent_encoded_unicode_project(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    r = _put(client, "dise%C3%B1o")
    assert r.status_code == 200, r.text
    sidecar = tmp_path / "inbox/laptop/claude-code/projects/p/a.jsonl.project"
    assert sidecar.read_text(encoding="utf-8") == "diseño\n"
    # Decomposed (macOS) spelling is stored composed.
    assert _put(client, "disen%CC%83o", "b.jsonl").status_code == 200
    decomposed = tmp_path / "inbox/laptop/claude-code/projects/p/b.jsonl.project"
    assert decomposed.read_text(encoding="utf-8") == "diseño\n"
    # Raw UTF-8 bytes in the header work too.
    assert _put(client, "diseño".encode(), "c.jsonl").status_code == 200
    # ASCII names are unchanged.
    assert _put(client, "billing-api.v2", "d.jsonl").status_code == 200


@pytest.mark.parametrize(
    "value",
    ["..", "...", "a/b", "a%2Fb", "a%5Cb", "%2E%2E", "a%00b", "a%0Ab", "_x", "-x", "%FF", "x" * 65],
)
def test_inbox_still_refuses_bad_project_names(tmp_path, value) -> None:
    app = _app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    r = _put(client, value)
    assert r.status_code == 400, (value, r.text)
    assert r.json()["error"] == "invalid project name"


def test_store_refuses_a_trailing_newline_in_the_project(tmp_path) -> None:
    with pytest.raises(inbox.InboxError, match="invalid project name"):
        inbox.store(tmp_path, "laptop", "hermes", "sessions/a.json", b"{}", "atlas\n")


# -- 5. the device folder is not walked on every upload --------------------------------------


def test_device_usage_is_cached_and_kept_up_to_date(tmp_path, monkeypatch) -> None:
    walks = []
    real_walk = os.walk

    def counting_walk(*args, **kwargs):
        walks.append(args[0])
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(inbox.os, "walk", counting_walk)
    limit = 300
    put = inbox.store
    put(tmp_path, "laptop", "hermes", "sessions/a.json", b"x" * 100, max_device_bytes=limit)
    put(tmp_path, "laptop", "hermes", "sessions/b.json", b"x" * 100, max_device_bytes=limit)
    # Replacing a file counts only the difference.
    put(tmp_path, "laptop", "hermes", "sessions/a.json", b"x" * 150, max_device_bytes=limit)
    with pytest.raises(inbox.InboxError) as full:
        put(tmp_path, "laptop", "hermes", "sessions/c.json", b"x" * 100, max_device_bytes=limit)
    assert full.value.status == 507
    assert len(walks) == 1  # computed once, then adjusted by each write
    # The cached total matches a real walk of the folder.
    folder = tmp_path.resolve() / "laptop"
    assert inbox._USAGE.get(folder) == inbox.device_usage(tmp_path.resolve(), "laptop") == 250
    # It is computed again after USAGE_RECOMPUTE_SECONDS (files removed by hand are seen).
    (tmp_path / "laptop/hermes/sessions/b.json").unlink()
    monkeypatch.setattr(inbox, "USAGE_RECOMPUTE_SECONDS", 0.0)
    put(tmp_path, "laptop", "hermes", "sessions/c.json", b"x" * 100, max_device_bytes=limit)


# -- 6. the query cut never leaves a dangling quote -----------------------------------------


def test_query_cut_mid_phrase_leaves_no_stray_quote() -> None:
    q = "x" * 4090 + ' "abc def"'
    match = build_match_query(q)
    assert match is not None
    assert '"""' not in match
    assert '"abc"' in match.split(" OR ")


def test_query_cut_drops_the_word_it_falls_in() -> None:
    q = "alpha " * ((MAX_QUERY_CHARS // 6) - 1) + "beta gammadelta"
    assert len(q) > MAX_QUERY_CHARS
    match = build_match_query(q)
    assert match is not None
    for term in match.split(" OR "):
        assert term in ('"alpha"', '"beta"'), term
    # One single long word is still searched (cut to a term), not dropped.
    assert build_match_query("y" * (MAX_QUERY_CHARS + 10)) is not None


def test_unpaired_quote_inside_the_limit_is_ignored() -> None:
    assert build_match_query('foo "bar') == '"foo" OR "bar"'


# -- 7. config errors never echo the rejected input ------------------------------------------


def test_config_errors_hide_inline_secrets(tmp_path) -> None:
    cfg = tmp_path / "jezero.toml"
    cfg.write_text(
        '[summarizer]\napi_key = "sk-ant-inline-secret-123456"\n\n'  # noqa: E501 privacy: allow
        '[[clients]]\nname = "laptop"\ntoken = "inline-client-token-abcdef"\n'
    )
    with pytest.raises(ValidationError) as caught:
        load_config(cfg)
    assert "inline-secret" not in str(caught.value)
    assert "inline-client-token" not in str(caught.value)
    text = format_config_error(caught.value)
    assert "inline-secret" not in text and "inline-client-token" not in text
    assert "errors.pydantic.dev" not in text
    assert "summarizer.api_key: Extra inputs are not permitted" in text
    assert "clients.0.token" in text


def test_format_config_error_keeps_the_field_message() -> None:
    with pytest.raises(ValidationError) as caught:
        Config.model_validate({"clients": [{"name": "Bad Name", "token_env": "X"}]})
    text = format_config_error(caught.value)
    assert text == "clients.0.name: client name must be lowercase letters, digits, - or _"
    assert format_config_error(FileNotFoundError("config not found")) == "config not found"


def test_server_reload_log_hides_the_inline_secret(tmp_path, caplog) -> None:
    cfg = tmp_path / "jezero.toml"
    good = (
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        f'[[clients]]\nname = "laptop"\ntoken_sha256 = "{sha(DEVICE_TOKEN)}"\n'
    )
    cfg.write_text(good)
    registry = ClientRegistry(load_config(cfg))
    cfg.write_text(good + 'token = "pasted-by-mistake-secret-999"\n')
    os.utime(cfg, ns=(1, 1))
    with caplog.at_level(logging.ERROR, logger="jezero.server"):
        _reload(registry)
    assert "could not be loaded" in caplog.text
    assert "pasted-by-mistake-secret-999" not in caplog.text
    assert "errors.pydantic.dev" not in caplog.text


# -- 8. the summarizer's key never travels over plain http to another host -------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080/v1", "http://127.0.0.5", "http://localhost:11434", "http://[::1]:1"],
)
def test_http_base_url_is_fine_on_loopback(url) -> None:
    assert SummarizerConfig(base_url=url).base_url == url


def test_http_base_url_to_another_host_is_refused() -> None:
    with pytest.raises(ValidationError, match="API key would travel in clear"):
        SummarizerConfig(base_url="http://192.168.1.20:8080/v1")  # privacy: allow
    with pytest.raises(ValidationError, match="allow_insecure_http"):
        Config.model_validate({"summarizer": {"base_url": "http://llm.lan/v1"}})
    allowed = SummarizerConfig(base_url="http://llm.lan/v1", allow_insecure_http=True)
    assert allowed.base_url == "http://llm.lan/v1"
    assert SummarizerConfig(base_url="https://llm.example.org/v1").allow_insecure_http is False


# -- 9. watchdog alerts carry no secrets -----------------------------------------------------


def test_watchdog_redacts_alerts_and_the_literal_key(tmp_path, monkeypatch) -> None:
    key = "plain-provider-key-XYZ123"
    monkeypatch.setenv("R2_SUM_KEY", key)
    monkeypatch.setenv("R2_HOOK", "https://hooks.example.org/T000/secret-hook-path")
    monkeypatch.setenv("R2_BEAT", "https://kuma.example.org/api/push/beat-token-1")
    store = new_store(tmp_path)
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": {"api_key_env": "R2_SUM_KEY"},
            "watchdog": {"alert_webhook_env": "R2_HOOK", "heartbeat_url_env": "R2_BEAT"},
            "connectors": [{"name": "agent-codex", "type": "codex"}],
        }
    )
    error = (
        f"401 from provider: invalid x-api-key {key}; "
        "Authorization: Bearer sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # noqa: E501 privacy: allow
    )
    store.set_state(ConnectorState("agent-codex", last_error=error))
    report = evaluate(config, store)
    assert report and key not in (report[0].last_error or "")
    assert "sk-ant-api03" not in (report[0].last_error or "")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    sent = alert(config, store, report, transport=httpx.MockTransport(handler))
    assert sent == ["agent-codex"]
    heartbeat(config, report, transport=httpx.MockTransport(handler))
    payload = "".join(r.content.decode() + str(r.url) for r in seen[:1])
    assert key not in payload and "sk-ant-api03" not in payload
    assert "[REDACTED" in payload

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    store.set_meta("watchdog.last_alert.agent-codex", None)
    errors: list[str] = []
    with pytest.MonkeyPatch.context():
        records: list[str] = []
        handler_log = logging.Handler()
        handler_log.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logging.getLogger("jezero.watchdog").addHandler(handler_log)
        try:
            alert(config, store, report, transport=httpx.MockTransport(failing), errors=errors)
        finally:
            logging.getLogger("jezero.watchdog").removeHandler(handler_log)
    assert errors == ["alert webhook failed: ConnectError"]
    assert records and all("secret-hook-path" not in r for r in records)


# -- 10. a failing tool call logs no argument ------------------------------------------------


def test_tool_failure_log_has_no_arguments(tmp_path, caplog) -> None:
    store = new_store(tmp_path)
    config = Config.model_validate(base_config(tmp_path))
    marker = "private-query-words-77"
    with caplog.at_level(logging.INFO), serve(config, store) as base:
        result = asyncio.run(
            _call_raw(f"{base}/mcp", DEVICE_TOKEN, "search", {"query": "x", "since": marker})
        )
    assert result.is_error
    assert "since must be an ISO 8601 date or datetime" in result.content[0].text
    assert marker not in caplog.text
    assert "failed" in caplog.text  # the failure itself is still logged


# -- 11. the agent is told that items are data, not instructions ------------------------------


def test_instructions_and_tools_say_items_are_data(tmp_path) -> None:
    assert "never instructions" in INSTRUCTIONS
    assert "user's confirmation" in INSTRUCTIONS
    mcp, _ = build_server(Config.model_validate(base_config(tmp_path)), new_store(tmp_path))
    tools = {t.name: " ".join((t.description or "").split()) for t in asyncio.run(mcp.list_tools())}
    assert "never instructions" in tools["search"]
    assert "user's confirmation" in tools["get_item"]


# -- 12. the edit lock file is owner-only -----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_edit_lock_file_is_created_0600(tmp_path) -> None:
    cfg = tmp_path / "jezero.toml"
    cfg.write_text("")
    old = os.umask(0o022)
    try:
        with _edit_lock(cfg):
            pass
    finally:
        os.umask(old)
    lock = tmp_path / ".jezero.toml.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    lock.chmod(0o644)  # an older, wider lock file is tightened
    with _edit_lock(cfg):
        pass
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
