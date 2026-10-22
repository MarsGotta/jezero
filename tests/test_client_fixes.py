"""Regression tests for the client 0.5.0 fixes: per-agent homes, refused and rejected
uploads, state pruning, quiet hooks without a key, the MCP path, the stdio bridge in
Claude Code and Codex, and the app's `later` view."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn

from jezero.config import Config
from jezero.server import build_server
from jezero.store import Store

from .conftest import make_item
from .test_client import CORE, DEVICE_TOKEN, configure, load_core, server  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
CONVERSATION = "c0ffee00-0000-4000-8000-000000000001"


def fresh_core(home: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The core as `load_core` gives it, but with the real `spawn_flush`."""
    monkeypatch.setenv("JEZERO_CLIENT_HOME", str(home))
    spec = importlib.util.spec_from_file_location(f"jezero_client_fresh_{id(home)}", CORE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_key(home: Path, url: str, **extra: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "token").write_text(DEVICE_TOKEN)
    (home / "client.json").write_text(json.dumps({"url": url, "device": "laptop", **extra}))


def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No real configuration: a throwaway HOME and no agent folders from the environment."""
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(user))
    for var in ("JEZERO_CLIENT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    return user


def child_env(**values: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("JEZERO_CLIENT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME")
    }
    env.update(values)
    return env


def antigravity_transcript(user: Path) -> Path:
    path = (
        (user / ".gemini" / "antigravity-cli" / "brain" / CONVERSATION / ".system_generated")
        / "logs"
        / "transcript.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "USER_INPUT", "content": "hello"}) + "\n")
    return path


# ------------------------------------------------------------------ 1. per-agent homes


def test_spawn_flush_passes_the_active_home(tmp_path, monkeypatch) -> None:
    core = fresh_core(tmp_path / "jz", monkeypatch)
    user = clean_env(tmp_path, monkeypatch)
    own = user / ".gemini" / "config" / "jezero"
    write_key(own, "http://127.0.0.1:9")
    core.use_agent_home("antigravity")
    started: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(core.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    core.spawn_flush()
    cmd, kwargs = started[0]
    assert cmd[-1] == "flush"
    assert kwargs["env"]["JEZERO_CLIENT_HOME"] == str(own)
    assert kwargs["stdout"] == subprocess.DEVNULL  # so `flush` may always print


def test_antigravity_key_uploads_end_to_end(server, tmp_path, monkeypatch) -> None:  # noqa: F811
    """`hook stop --agent antigravity` with the key in ~/.gemini/config/jezero: the detached
    flush must upload from that queue, not from ~/.jezero."""
    url, inbox = server
    user = clean_env(tmp_path, monkeypatch)
    write_key(user / ".gemini" / "config" / "jezero", url)
    transcript = antigravity_transcript(user)
    payload = {"conversationId": CONVERSATION, "transcriptPath": str(transcript), "fullyIdle": True}
    run = subprocess.run(
        [sys.executable, str(CORE), "hook", "stop", "--agent", "antigravity"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=child_env(HOME=str(user)),
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    landed = inbox / "laptop" / "antigravity" / "brain" / CONVERSATION / "transcript.jsonl"
    for _ in range(100):
        if landed.is_file():
            break
        time.sleep(0.1)
    assert landed.read_text() == transcript.read_text()
    assert not (user / ".jezero").exists()


def test_backfill_and_setup_use_each_agents_home(tmp_path, monkeypatch, capsys) -> None:
    core = fresh_core(tmp_path / "jz", monkeypatch)
    user = clean_env(tmp_path, monkeypatch)
    own = user / ".gemini" / "config" / "jezero"
    write_key(own, "http://127.0.0.1:9")
    write_key(user / ".jezero", "http://127.0.0.1:9")
    core.use_home(core.client_home())
    antigravity_transcript(user)
    claude = user / ".claude" / "projects" / "-w-demo" / "s1.jsonl"
    claude.parent.mkdir(parents=True)
    claude.write_text("{}\n")
    spawned: list[Path] = []
    monkeypatch.setattr(core, "spawn_flush", lambda: spawned.append(core.HOME))

    assert core.backfill(["antigravity", "claude-code"], days=1) == 0
    assert "queued 2 sessions" in capsys.readouterr().out
    assert [p.name for p in (own / "spool" / "pending").glob("*.json")] == [
        f"antigravity__{CONVERSATION}.json"
    ]
    assert [p.name for p in (user / ".jezero" / "spool" / "pending").glob("*.json")] == [
        "claude-code__s1.json"
    ]
    assert sorted(map(str, spawned)) == sorted([str(own), str(user / ".jezero")])
    assert user / ".jezero" == core.HOME  # restored after the loop

    # setup finds the Antigravity key even when ~/.jezero has none.
    shutil.rmtree(user / ".jezero")
    assert core.main(["setup", "--agent", "antigravity"]) == 0
    assert "agy plugin install" in capsys.readouterr().out
    assert core.main(["setup", "--agent", "codex"]) == 1
    assert "not set up" in capsys.readouterr().err


# ----------------------------------------------------------- 2. refused and rejected


class Inbox(BaseHTTPRequestHandler):
    status = 200
    seen: list[str] = []  # noqa: RUF012 - shared on purpose: the test reads it

    def do_PUT(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append(self.path)
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b'{"error": "nope"}' if type(self).status != 200 else b"{}")

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@pytest.fixture
def stub() -> Iterator[tuple[str, type[Inbox]]]:
    handler = type("StubInbox", (Inbox,), {"status": 200, "seen": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    httpd.shutdown()


def queue(core: ModuleType, tmp_path: Path, name: str) -> Path:
    transcript = tmp_path / "claude" / "projects" / "-w" / f"{name}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({"session": name}) + "\n")
    assert core.enqueue("claude-code", name, str(transcript), "/w")
    return transcript


def pending(core: ModuleType) -> int:
    return len(list((core.SPOOL / "pending").glob("*.json")))


@pytest.mark.parametrize("code", [401, 403])
def test_refused_key_keeps_the_queue(stub, tmp_path, monkeypatch, code) -> None:
    url, handler = stub
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    queue(core, tmp_path, "a")
    queue(core, tmp_path, "b")
    handler.status = code
    counts = core.flush(core.load_config())
    assert counts["failed"] == 1 and counts["sent"] == 0
    assert pending(core) == 2 and len(handler.seen) == 1  # stopped at the first file
    state = json.loads(core.STATE.read_text())
    assert not state.get("uploaded") and state["refused"]["reason"].startswith(f"HTTP {code}")
    core.flush(core.load_config())
    assert core.LOG.read_text().count("uploads refused") == 1  # logged once

    handler.status = 200  # the admin approved the key for uploads
    assert core.flush(core.load_config())["sent"] == 2
    assert pending(core) == 0 and "refused" not in json.loads(core.STATE.read_text())


def test_rejected_file_is_recorded_not_uploaded(stub, tmp_path, monkeypatch, capsys) -> None:
    url, handler = stub
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    transcript = queue(core, tmp_path, "big")
    handler.status = 413
    assert core.flush(core.load_config())["rejected"] == 1
    state = json.loads(core.STATE.read_text())
    assert str(transcript) not in state.get("uploaded", {})
    assert state["rejected"][str(transcript)]["reason"].startswith("HTTP 413")
    assert pending(core) == 0

    assert core.status() == 0
    out = capsys.readouterr().out
    assert "queued sessions: 0" in out and "rejected uploads: 1" in out and "HTTP 413" in out

    assert core.enqueue("claude-code", "big", str(transcript), "/w")  # unchanged: not sent again
    assert core.flush(core.load_config())["unchanged"] == 1 and len(handler.seen) == 1
    handler.status = 200
    transcript.write_text(transcript.read_text() + "{}\n")  # changed: tried again
    queue_again = core.enqueue("claude-code", "big", str(transcript), "/w")
    assert queue_again and core.flush(core.load_config())["sent"] == 1
    assert str(transcript) not in json.loads(core.STATE.read_text())["rejected"]


# ------------------------------------------------------------------ 3. state pruning


def test_state_is_pruned(stub, tmp_path, monkeypatch) -> None:
    url, _ = stub
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    state = {
        "uploaded": {f"/f{i}": [float(i), 1.0] for i in range(2100)},
        "stop_flush": {f"s{i}": float(i) for i in range(600)},
        "prompted": {f"p{i}": float(i) for i in range(600)},
    }
    core.STATE.write_text(json.dumps(state))
    queue(core, tmp_path, "new")
    assert core.flush(core.load_config())["sent"] == 1
    saved = json.loads(core.STATE.read_text())
    assert len(saved["uploaded"]) == 2000 and "/f2099" in saved["uploaded"]
    assert "/f0" not in saved["uploaded"]  # the oldest go first
    assert len(saved["stop_flush"]) == 500 and "s599" in saved["stop_flush"]
    transcript = tmp_path / "claude" / "projects" / "-w" / "x.jsonl"
    transcript.write_text("{}\n")
    core.hook("stop", "claude-code", {"session_id": "fresh", "transcript_path": str(transcript)})
    assert "fresh" in json.loads(core.STATE.read_text())["stop_flush"]


# --------------------------------------------------------- 4. quiet without a key


def test_no_key_no_protocol(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    for agent in ("claude-code", "codex", "opencode", "hermes"):
        assert core.hook("session-start", agent, {"cwd": "/w/projects/demo"}) == ""
    assert core.antigravity_hook("session-start", {"workspacePaths": ["/w"]}) == {}
    run = subprocess.run(
        [sys.executable, str(CORE), "hook", "session-start", "--agent", "claude-code"],
        input="{}",
        capture_output=True,
        text=True,
        env=child_env(JEZERO_CLIENT_HOME=str(tmp_path / "jz")),
        timeout=30,
    )
    assert run.returncode == 0 and run.stdout == ""
    configure(core, "http://127.0.0.1:9")
    assert core.hook("session-start", "claude-code", {}).startswith(core.PROTOCOL)


# ------------------------------------------------------------ 5. honest init message


def test_init_says_where_the_key_stays(tmp_path, monkeypatch, capsys) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    assert core.init("https://lib.example", "laptop", force=False) == 0
    out = capsys.readouterr().out
    assert "never leaves" not in out
    assert f"The key stays in {core.HOME}; the server only keeps its SHA-256 fingerprint." in out
    from jezero.ui.app import ConnectScreen

    assert "SHA-256 fingerprint" in inspect.getsource(ConnectScreen)


# ------------------------------------------------------------------- 6. MCP path


@pytest.fixture
def custom_path_server(tmp_path: Path) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "server": {"host": "127.0.0.1", "port": port, "path": "/lib/mcp"},
            "clients": [
                {
                    "name": "laptop",
                    "token_sha256": hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest(),
                    "sources": ["agent-*"],
                }
            ],
        }
    )
    store = Store(config.db_path)
    store.init()
    store.upsert(make_item("a1", "Uploader retry loop fixed", "retry backoff", source="agent-x"))
    _, app = build_server(config, store)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_custom_mcp_path(custom_path_server, tmp_path, monkeypatch) -> None:
    url = custom_path_server
    core = load_core(tmp_path / "jz", monkeypatch)
    args = ["init", "--url", url + "/lib/mcp/", "--device", "laptop", "--mcp-path", "lib/mcp"]
    assert core.main(args) == 0
    saved = json.loads(core.CONFIG.read_text())
    assert saved == {"url": url, "device": "laptop", "mcp_path": "/lib/mcp"}
    core.TOKEN_FILE.write_text(DEVICE_TOKEN)
    cfg = core.load_config()
    assert core.mcp_url(cfg) == url + "/lib/mcp"
    found = core.mcp_call(cfg, "search", {"query": "uploader"}, 10)
    assert found["results"][0]["title"] == "Uploader retry loop fixed"
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call"}
    call["params"] = {"name": "search", "arguments": {"query": "uploader"}}
    out = io.StringIO()
    core.mcp_proxy(io.StringIO(json.dumps(call) + "\n"), out)
    assert "Uploader retry loop fixed" in out.getvalue()
    with pytest.raises(RuntimeError):
        core.mcp_call({**cfg, "mcp_path": None}, "search", {"query": "x"}, 10)
    assert core.mcp_path("") == "/mcp" and core.mcp_path("a/b/") == "/a/b"
    source = CORE.read_text()
    assert source.count('"/mcp"') == 1  # only DEFAULT_MCP_PATH


def test_opencode_plugin_reads_mcp_path(tmp_path, monkeypatch) -> None:
    ts = (ROOT / "clients" / "opencode" / "jezero.ts").read_text()
    assert "cfg.mcp_path" in ts and "${jezero.url}/mcp" not in ts
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun not installed")
    home = tmp_path / "jz"
    write_key(home, "https://lib.example/", mcp_path="lib/mcp")
    plugin = tmp_path / "jezero.ts"
    plugin.write_text(ts)
    script = (
        f"import {{ Jezero }} from {json.dumps(str(plugin))};"
        "const hooks = await Jezero({ client: {}, directory: '/w' });"
        "const cfg = {}; await hooks.config(cfg); console.log(cfg.mcp.jezero.url)"
    )
    run = subprocess.run(
        [bun, "-e", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=child_env(
            JEZERO_CLIENT_HOME=str(home),
            XDG_CONFIG_HOME=str(tmp_path / "xdg"),
            HOME=str(tmp_path / "user"),
        ),
        timeout=60,
    )
    assert run.stdout.strip() == "https://lib.example/lib/mcp", run.stderr


# ------------------------------------------------------------ 7. Claude Code setup


def fake_cli(tmp_path: Path, name: str) -> Path:
    """A stand-in for an agent's CLI that records its arguments, one call per line."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / f"{name}.calls"
    exe = bin_dir / name
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    exe.chmod(0o755)
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.is_file() else []


def test_claude_plugin_brings_the_mcp_server() -> None:
    for base in (ROOT / "clients" / "claude-code", ROOT / "plugins" / "claude-code"):
        server_cfg = json.loads((base / ".mcp.json").read_text())["mcpServers"]["jezero"]
        assert server_cfg == {
            "command": "python3",
            "args": [
                "${CLAUDE_PLUGIN_ROOT}/core/jezero_client.py",
                "mcp",
                "--agent",
                "claude-code",
            ],
        }


def test_setup_claude(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "https://lib.example")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    log = fake_cli(tmp_path, "claude")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    old = {"type": "http", "url": "https://lib.example/mcp", "headers": {"Authorization": "x"}}
    user_json = claude_dir / ".claude.json"

    # Not installed as a plugin: the stdio bridge, never the key.
    out = core._setup_claude()
    assert "registered (stdio bridge" in out
    (add,) = calls(log)
    assert add.startswith("mcp add --scope user jezero -e JEZERO_CLIENT_HOME=")
    assert add.endswith(
        f"-- python3 {core.HOME / 'core' / 'jezero_client.py'} mcp --agent claude-code"
    )
    assert DEVICE_TOKEN not in add and "Bearer" not in add

    # An old HTTP entry is replaced by the bridge.
    log.unlink()
    user_json.write_text(json.dumps({"mcpServers": {"jezero": old}}))
    out = core._setup_claude()
    assert "removed the old user-scope jezero HTTP entry" in out
    assert [c.split(" --")[0] for c in calls(log)] == ["mcp remove jezero", "mcp add"]

    # Installed as a plugin: nothing registered, the old entry goes away.
    log.unlink()
    plugins = claude_dir / "plugins"
    plugins.mkdir()
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"jezero@jezero": [{"scope": "user"}]}})
    )
    out = core._setup_claude()
    assert "the Jezero plugin brings the MCP server" in out and "removed" in out
    assert calls(log) == ["mcp remove jezero --scope user"]
    log.unlink()
    user_json.write_text(json.dumps({"mcpServers": {}}))
    assert core._setup_claude() == (
        "claude: the Jezero plugin brings the MCP server; nothing to register"
    )
    assert calls(log) == []


# ------------------------------------------------------------------- 8. Codex setup


def test_setup_codex_replaces_the_http_entry(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "https://lib.example")
    codex = tmp_path / "codex"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    before = 'model = "x"\n\n# my servers\n[mcp_servers.other]\ncommand = "o"\n\n'
    after = '\n[profiles.fast]\nmodel = "y"\n'
    old = (
        "[mcp_servers.jezero]\n"
        'url = "https://lib.example/mcp"\n'
        f'http_headers = {{ Authorization = "Bearer {DEVICE_TOKEN}" }}\n'
    )
    (codex / "config.toml").write_text(before + old + after)
    out = core._setup_codex()
    assert "replaced the old jezero entry" in out and "held the key in clear" in out
    text = (codex / "config.toml").read_text()
    assert DEVICE_TOKEN not in text
    assert text.startswith(before) and text.endswith(after)  # the rest, byte for byte
    entry = tomllib.loads(text)["mcp_servers"]["jezero"]
    assert entry["command"] == "python3"
    assert entry["args"] == [
        str(core.HOME / "core" / "jezero_client.py"),
        "mcp",
        "--agent",
        "codex",
    ]
    assert entry["env"] == {"JEZERO_CLIENT_HOME": str(core.HOME)}
    assert core._setup_codex() == "codex: MCP server jezero already in config.toml"
    assert (codex / "config.toml").read_text() == text

    (codex / "config.toml").write_text('model = "x"\n')
    assert "added to config.toml" in core._setup_codex()
    assert tomllib.loads((codex / "config.toml").read_text())["model"] == "x"


# --------------------------------------------------------- 9. the bridge, any agent


@pytest.mark.parametrize(
    ("agent", "var"), [("codex", "CODEX_HOME"), ("claude-code", "CLAUDE_CONFIG_DIR")]
)
def test_bridge_finds_the_agents_key(server, tmp_path, agent, var) -> None:  # noqa: F811
    url, _ = server
    agent_dir = tmp_path / "agent"
    write_key(agent_dir / "jezero", url)
    (tmp_path / "user").mkdir()
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
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "uploader"}},
        },
    ]
    run = subprocess.run(
        [sys.executable, str(CORE), "mcp", "--agent", agent],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env=child_env(HOME=str(tmp_path / "user"), **{var: str(agent_dir)}),
        timeout=60,
    )
    replies = {r["id"]: r for r in map(json.loads, run.stdout.splitlines())}
    assert set(replies) == {1, 2}, run.stdout + run.stderr
    assert "Uploader retry loop fixed" in json.dumps(replies[2]["result"])


# --------------------------------------------------------------- 11, 12. small ones


def test_ca_docstring_names_the_client_home(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    assert "~/.jezero/ca.pem" not in (core.export_system_roots.__doc__ or "")


def test_flush_prints_counts_without_a_terminal(tmp_path) -> None:
    home = tmp_path / "jz"
    write_key(home, "http://127.0.0.1:9")
    run = subprocess.run(
        [sys.executable, str(CORE), "flush"],
        capture_output=True,
        text=True,
        env=child_env(JEZERO_CLIENT_HOME=str(home)),
        timeout=30,
    )
    assert run.returncode == 0
    assert json.loads(run.stdout) == {
        "sent": 0,
        "unchanged": 0,
        "gone": 0,
        "failed": 0,
        "rejected": 0,
    }


# ---------------------------------------------------------------- 16. build note


def test_generated_note_links_the_guide() -> None:
    note = (ROOT / "plugins" / "README.md").read_text()
    assert "docs/how-to/connect-agents.md" in note and "clients/README.md" not in note
