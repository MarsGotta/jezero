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
            ],
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
