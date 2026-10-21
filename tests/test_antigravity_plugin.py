"""Antigravity end to end: the plugin's files, its hooks, the stdio MCP bridge, the
upload and the connector that reads the uploaded transcripts."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from jezero.connectors.antigravity import AntigravityConnector
from jezero.inbox import AGENTS as INBOX_AGENTS
from jezero.store import ConnectorState

from .test_client import configure, load_core, server  # noqa: F401  (server: fixture)

ROOT = Path(__file__).resolve().parent.parent
CONVERSATION = "ec33ebf9-0cba-4100-8142-c61503f6c587"


def write_transcript(home: Path, request: str) -> Path:
    path = home / "brain" / CONVERSATION / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True)
    steps = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-10-21T10:00:00Z",
            "content": f"<USER_REQUEST>\n{request}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
            "time and settings\n</ADDITIONAL_METADATA>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-21T10:00:05Z",
            "content": "",
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": "pytest -q tests/upload"}},
                {"name": "view_file", "args": {"AbsolutePath": "/w/a.py"}},
            ],
        },
        {"step_index": 2, "type": "GENERIC", "content": "huge tool output"},
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-21T10:01:00Z",
            "content": "The backoff never resets after a success; fixed in uploader.py. " * 8,
        },
    ]
    path.write_text("".join(json.dumps(s) + "\n" for s in steps))
    return path


def test_plugin_files() -> None:
    base = ROOT / "plugins" / "antigravity"
    manifest = json.loads((base / "plugin.json").read_text())
    assert set(manifest) <= {"$schema", "name", "description"}  # the schema allows no more
    assert manifest["name"] == "jezero"
    hooks = json.loads((base / "hooks.json").read_text())["jezero"]
    assert set(hooks) == {"SessionStart", "PreInvocation", "Stop"}
    for handlers in hooks.values():
        assert handlers[0]["command"].startswith("python3 ./core/jezero_client.py hook ")
    server_cfg = json.loads((base / "mcp_config.json").read_text())["mcpServers"]["jezero"]
    assert server_cfg["args"][:2] == ["./core/jezero_client.py", "mcp"]
    assert "headers" not in server_cfg  # the key never sits in Antigravity's config
    assert (base / "core" / "jezero_client.py").is_file()
    assert (base / "skills" / "jezero" / "SKILL.md").is_file()


def test_key_in_antigravity_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    monkeypatch.delenv("JEZERO_CLIENT_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    own = tmp_path / "user" / ".gemini" / "config" / "jezero"
    core.use_agent_home("claude-code")
    assert own != core.HOME
    core.use_agent_home("antigravity")
    assert own != core.HOME  # no key there yet
    own.mkdir(parents=True)
    (own / "client.json").write_text("{}")
    core.use_agent_home("antigravity")
    assert own == core.HOME
    assert own / "client.json" == core.CONFIG


def test_hooks_end_to_end(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    url, inbox = server
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    transcript = write_transcript(tmp_path / "agy", "the uploader retry backoff is broken")
    common = {
        "conversationId": CONVERSATION,
        "workspacePaths": ["/home/me/projects/atlas"],
        "transcriptPath": str(transcript),
    }

    start = core.antigravity_hook("session-start", common)
    text = start["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Jezero indexes past sessions" in text  # the protocol, every time

    first = core.antigravity_hook("user-prompt", {**common, "invocationNum": 0})
    related = first["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Uploader retry loop fixed" in related
    assert core.antigravity_hook("user-prompt", {**common, "invocationNum": 1}) == {}
    assert core.antigravity_hook("user-prompt", {**common, "invocationNum": 0}) == {}  # once

    assert core.antigravity_hook("stop", {**common, "fullyIdle": False}) == {}
    assert not list((core.SPOOL / "pending").glob("*.json"))
    assert core.antigravity_hook("stop", {**common, "fullyIdle": True}) == {}
    assert core.flush(core.load_config())["sent"] == 1
    landed = inbox / "laptop" / "antigravity" / "brain" / CONVERSATION / "transcript.jsonl"
    assert landed.read_text() == transcript.read_text()
    assert (landed.parent / "transcript.jsonl.project").read_text().strip() == "atlas"
    assert INBOX_AGENTS["antigravity"].fullmatch(f"brain/{CONVERSATION}/transcript.jsonl")

    later = time.time() + 3 * 3600
    batch = AntigravityConnector(
        "agent-antigravity-laptop",
        {"transcripts_dir": str(inbox / "laptop" / "antigravity" / "brain"), "min_chars": 50},
        max_segment_chars=10_000,
    ).collect(ConnectorState("x"), now=later)
    doc = batch.docs[0]
    assert doc.external_id == f"{CONVERSATION}#0"
    assert doc.project_hint == "atlas"
    assert "user: the uploader retry backoff is broken" in doc.text
    assert "shell: pytest -q tests/upload" in doc.text
    assert "assistant: The backoff never resets" in doc.text
    assert "ADDITIONAL_METADATA" not in doc.text and "huge tool output" not in doc.text
    assert "a.py" not in doc.text  # reads are dropped, as in the local store reader
    assert batch.next_cursor is not None and not batch.warnings


def test_mcp_bridge(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    url, _ = server
    core = load_core(tmp_path / "jz", monkeypatch)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "uploader"}},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))

    out = io.StringIO()
    core.mcp_proxy(io.StringIO(stdin.getvalue()), out)  # not connected yet: clear errors
    errors = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["id"] for e in errors] == [1, 2, 3]
    assert "init" in errors[0]["error"]["message"]

    configure(core, url)
    out = io.StringIO()
    core.mcp_proxy(stdin, out)
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert set(replies) == {1, 2, 3}  # the notification gets no reply
    tools = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"search", "get_item", "later"} <= tools
    assert "Uploader retry loop fixed" in json.dumps(replies[3]["result"])
