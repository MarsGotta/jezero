"""End to end: a real uvicorn server, a real MCP client, real bearer tokens."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from jezero.config import Config
from jezero.server import build_server
from jezero.store import Store

from .conftest import make_item

AGENT_TOKEN = "agent-token-for-tests-only"
ADMIN_TOKEN = "admin-token-for-tests-only"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url(tmp_path, monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("TEST_AGENT_TOKEN", AGENT_TOKEN)
    port = _free_port()
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "server": {"host": "127.0.0.1", "port": port},
            "clients": [
                {"name": "agent", "token_env": "TEST_AGENT_TOKEN", "sources": ["agent-*"]},
                {"name": "admin", "token_sha256": hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()},
                {
                    "name": "laptop",
                    "token_sha256": hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest(),
                    "sources": ["agent-*"],
                    "upload": True,
                },
            ],
            "inbox": {"dir": str(tmp_path / "inbox"), "max_mb": 0.01},
        }
    )
    store = Store(config.db_path)
    store.init()
    store.upsert(make_item("a1", "Agent session", "keyword from an agent", source="agent-x"))
    store.upsert(
        make_item(
            "m1", "Mail thread", "keyword from a mail", source="mail-personal", trust="external"
        )
    )
    _, app = build_server(config, store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


async def _call(url: str, token: str | None, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=10) as http,
        Client(streamable_http_client(url, http_client=http), cache=None) as client,
    ):
        result = await client.call_tool(tool, args)
        assert not result.is_error, result
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)


def call(url: str, token: str | None, tool: str, args: dict | None = None) -> dict:
    return asyncio.run(_call(url, token, tool, args or {}))


DEVICE_TOKEN = "device-token-for-tests"


def _base(url: str) -> str:
    return url.rsplit("/mcp", 1)[0]


def test_inbox_ping_and_upload(server_url: str, tmp_path) -> None:
    import gzip

    base = _base(server_url)
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    assert httpx2.get(f"{base}/inbox/v1/ping").status_code == 401
    assert (
        httpx2.get(f"{base}/inbox/v1/ping", headers={"Authorization": "Bearer x"}).status_code
        == 401
    )
    agent = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    assert httpx2.get(f"{base}/inbox/v1/ping", headers=agent).status_code == 403
    assert httpx2.get(f"{base}/inbox/v1/ping", headers=auth).json()["device"] == "laptop"

    url = f"{base}/inbox/v1/claude-code/projects/-w-proj/abc-123.jsonl"
    r = httpx2.put(url, content=b'{"type":"user"}\n', headers=auth)
    assert r.status_code == 200, r.text
    target = (
        tmp_path / "inbox" / "laptop" / "claude-code" / "projects" / "-w-proj" / "abc-123.jsonl"
    )
    assert target.read_bytes() == b'{"type":"user"}\n'
    body = gzip.compress(b'{"type":"assistant"}\n')
    r = httpx2.put(url, content=body, headers={**auth, "Content-Encoding": "gzip"})
    assert r.status_code == 200 and target.read_bytes() == b'{"type":"assistant"}\n'

    codex = f"{base}/inbox/v1/codex/sessions/2026/10/21/rollout-2026-10-21T10-00-00-x.jsonl.zst"
    assert httpx2.put(codex, content=b"zz", headers=auth).status_code == 200
    for bad in (
        f"{base}/inbox/v1/claude-code/projects/../../etc/passwd.jsonl",
        f"{base}/inbox/v1/claude-code/other/x.jsonl",
        f"{base}/inbox/v1/cursor/sessions/x.json",
        f"{base}/inbox/v1/opencode/sessions/x.txt",
    ):
        assert httpx2.put(bad, content=b"x", headers=auth).status_code in (400, 404), bad
    assert httpx2.put(url, content=b"x" * 20_000, headers=auth).status_code == 413
    bomb = gzip.compress(b"0" * 50_000)
    assert (
        httpx2.put(url, content=bomb, headers={**auth, "Content-Encoding": "gzip"}).status_code
        == 413
    )
    assert httpx2.put(url, content=b"x", headers=agent).status_code == 403


def test_rejects_missing_or_wrong_token(server_url: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}
    assert httpx2.post(server_url, json=body, headers=headers).status_code == 401
    wrong = {**headers, "Authorization": "Bearer nope"}
    assert httpx2.post(server_url, json=body, headers=wrong).status_code == 401


def test_agent_only_sees_allowed_sources(server_url: str) -> None:
    result = call(server_url, AGENT_TOKEN, "search", {"query": "keyword"})
    assert {r["source"] for r in result["results"]} == {"agent-x"}
    sources = call(server_url, AGENT_TOKEN, "list_sources")["sources"]
    assert set(sources) == {"agent-x"}


def test_admin_sees_everything_and_trust_is_exposed(server_url: str) -> None:
    result = call(server_url, ADMIN_TOKEN, "search", {"query": "keyword"})
    by_source = {r["source"]: r for r in result["results"]}
    assert set(by_source) == {"agent-x", "mail-personal"}
    assert by_source["mail-personal"]["trust"] == "external"


def test_get_item_and_timeline(server_url: str) -> None:
    hit = call(server_url, ADMIN_TOKEN, "search", {"query": "mail"})["results"][0]
    assert call(server_url, AGENT_TOKEN, "get_item", {"item_id": hit["id"]}) == {"found": False}
    full = call(server_url, ADMIN_TOKEN, "get_item", {"item_id": hit["id"]})
    assert full["found"] and full["item"]["title"] == "Mail thread"
    timeline = call(server_url, AGENT_TOKEN, "timeline", {})
    assert [i["source"] for i in timeline["items"]] == ["agent-x"]


def test_later_finds_the_decision_that_replaced_an_earlier_one(tmp_path) -> None:
    from jezero.server import later_items

    store = Store(tmp_path / "lib.db")
    store.init()
    store.upsert(
        make_item("p1", "Launch plan rev 4: shop moves to shop.example.org", "move", days_ago=3)
    )
    store.upsert(
        make_item("p2", "Launch plan rev 6: the shop stays on shop.example", "stay", days_ago=1)
    )
    store.upsert(make_item("x1", "Unrelated: printer driver", "other", days_ago=0.5))
    store.upsert(
        make_item("o1", "Launch plan in another project", "x", days_ago=0.5, project="other")
    )
    store.upsert(make_item("old", "Launch plan rev 1", "first", days_ago=9))
    for n, words in enumerate(["kernel panic", "invoice totals", "garden pump", "font sizes"]):
        store.upsert(make_item(f"f{n}", words, words, days_ago=20, project="filler"))
    by_title = {i.external_id: i for i in store.timeline(limit=10)}
    newer, how = later_items(store, by_title["p1"])
    assert ([i.external_id for i in newer], how) == (["p2"], "subject")
    # Nothing newer on the subject: the project's newer items, flagged as such.
    newer, how = later_items(store, by_title["p2"])
    assert ([i.external_id for i in newer], how) == (["x1"], "project_timeline")
    assert later_items(store, by_title["p1"], allowed_sources=["mail-*"]) == (
        [],
        "project_timeline",
    )


def test_device_approve_adds_client_and_disabled_connectors(tmp_path) -> None:
    from jezero.cli import main as cli_main
    from jezero.config import load_config

    cfg = tmp_path / "jezero.toml"
    cfg.write_text('[storage]\npath = "lib.db"\n')
    digest = hashlib.sha256(b"k").hexdigest()
    assert cli_main(["-c", str(cfg), "device", "approve", "my-laptop", f"sha256:{digest}"]) == 0
    config = load_config(cfg)
    (client,) = config.clients
    assert (client.name, client.upload, client.token_sha256) == ("my-laptop", True, digest)
    names = {c.name: c for c in config.connectors}
    assert set(names) == {
        "agent-claude-code-my-laptop",
        "agent-codex-my-laptop",
        "agent-opencode-my-laptop",
    }
    assert not any(c.enabled for c in names.values())
    assert (tmp_path / "data" / "inbox" / "my-laptop" / "codex" / "sessions").is_dir()
    assert cli_main(["-c", str(cfg), "device", "approve", "my-laptop", digest]) == 1
    assert cli_main(["-c", str(cfg), "device", "approve", "x", "nothex"]) == 2
