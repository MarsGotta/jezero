"""The shared client core (clients/core/jezero_client.py) against a real Jezero server."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn

from jezero.config import Config
from jezero.server import build_server
from jezero.store import Store

from .conftest import make_item

CORE = Path(__file__).resolve().parent.parent / "clients" / "core" / "jezero_client.py"
DEVICE_TOKEN = "client-test-device-token"


def load_core(home: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("JEZERO_CLIENT_HOME", str(home))
    spec = importlib.util.spec_from_file_location(f"jezero_client_{id(home)}", CORE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "spawn_flush", lambda: None)  # tests flush explicitly
    return module


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "server": {"host": "127.0.0.1", "port": port},
            "clients": [
                {
                    "name": "laptop",
                    "token_sha256": hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest(),
                    "sources": ["agent-*"],
                    "upload": True,
                }
            ],
            "inbox": {"dir": str(tmp_path / "inbox")},
        }
    )
    store = Store(config.db_path)
    store.init()
    store.upsert(
        make_item("a1", "Uploader retry loop fixed", "retry backoff in uploader", source="agent-x")
    )
    store.upsert(make_item("a2", "Other project work", "unrelated", source="agent-x", project="zz"))
    _, app = build_server(config, store)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", tmp_path / "inbox"
    srv.should_exit = True
    thread.join(timeout=5)


def configure(core: ModuleType, url: str) -> None:
    core.HOME.mkdir(parents=True, exist_ok=True)
    core.TOKEN_FILE.write_text(DEVICE_TOKEN)
    core.CONFIG.write_text(json.dumps({"url": url, "device": "laptop"}))


def test_paths_and_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = load_core(tmp_path / "home", monkeypatch)
    rel = core.relpath_for
    assert (
        rel("claude-code", Path("/u/.claude/projects/-w-p/abc.jsonl")) == "projects/-w-p/abc.jsonl"
    )
    assert rel("claude-code", Path("/u/.claude/projects/-w-p/abc/subagents/x.jsonl")) is None
    codex = Path("/u/.codex/sessions/2026/10/21/rollout-2026-10-21T10-00-00-ab.jsonl.zst")
    assert rel("codex", codex) == "sessions/2026/10/21/" + codex.name
    assert rel("codex", Path("/tmp/rollout-2026-10-21T10-00-00-ab.jsonl")).startswith(
        "sessions/2026/10/21/"
    )
    assert rel("codex", Path("/tmp/notes.jsonl")) is None
    assert rel("opencode", Path("/h/.jezero/spool/opencode/ses_1.json")) == "sessions/ses_1.json"
    assert core.project_from_cwd("/Users/me/projects/Atlas/src") == "atlas"
    assert core.project_from_cwd("/Users/me/code") is None


def test_init_keeps_the_key_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    core = load_core(tmp_path / "home", monkeypatch)
    assert core.init("https://lib.example/mcp", "my-laptop", force=False) == 0
    token = core.TOKEN_FILE.read_text()
    out = capsys.readouterr().out
    assert token not in out and hashlib.sha256(token.encode()).hexdigest() in out
    assert json.loads(core.CONFIG.read_text())["url"] == "https://lib.example"
    assert core.TOKEN_FILE.stat().st_mode & 0o077 == 0
    assert core.init("https://lib.example", "my-laptop", force=False) == 1


def test_hooks_context_and_upload(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url, inbox = server
    core = load_core(tmp_path / "home", monkeypatch)

    # Not configured yet: the protocol still goes in, nothing else happens.
    assert core.hook("session-start", "claude-code", {"cwd": "/w/projects/demo"}) == core.PROTOCOL
    configure(core, url)

    start = core.hook("session-start", "claude-code", {"cwd": "/w/projects/demo"})
    assert start.startswith(core.PROTOCOL) and "Uploader retry loop fixed" in start
    assert "Other project work" not in start

    prompt = {"session_id": "s1", "prompt": "the uploader retry backoff is broken again"}
    first = core.hook("user-prompt", "claude-code", prompt)
    assert "Uploader retry loop fixed" in first
    assert core.hook("user-prompt", "claude-code", prompt) == ""  # only the first prompt

    transcript = tmp_path / ".claude" / "projects" / "-w-projects-demo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user"}\n')
    end = {"session_id": "s1", "transcript_path": str(transcript), "cwd": "/w/projects/demo"}
    assert core.hook("session-end", "claude-code", end) == ""
    assert core.flush(core.load_config())["sent"] == 1
    landed = inbox / "laptop" / "claude-code" / "projects" / "-w-projects-demo" / "s1.jsonl"
    assert landed.read_text() == '{"type":"user"}\n'

    core.hook("session-end", "claude-code", end)
    assert core.flush(core.load_config())["unchanged"] == 1  # same file: not sent again


def test_offline_keeps_the_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = load_core(tmp_path / "home", monkeypatch)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    configure(core, f"http://127.0.0.1:{dead}")
    transcript = tmp_path / "projects" / "-w" / "s2.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    core.hook("stop", "claude-code", {"session_id": "s2", "transcript_path": str(transcript)})
    assert core.flush(core.load_config())["failed"] == 1
    assert len(list((core.SPOOL / "pending").glob("*.json"))) == 1
    # A failing server or a hook error never raises into the agent.
    assert core.hook("session-start", "codex", {"cwd": "/w/projects/demo"}) == core.PROTOCOL


def test_certificate_failure_exports_system_roots_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl
    import urllib.error

    core = load_core(tmp_path / "home", monkeypatch)
    calls: list[object] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout, context):  # noqa: ANN001
        calls.append(context)
        if len(calls) == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get issuer"))
        return Response()

    exported: list[bool] = []
    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(core, "export_system_roots", lambda: exported.append(True) or True)
    assert core._http("GET", "https://lib.example/inbox/v1/ping", "t") == (200, b"{}")
    assert len(calls) == 2 and exported == [True]

    monkeypatch.setattr(core, "export_system_roots", lambda: False)
    calls.clear()
    with pytest.raises(urllib.error.URLError):
        core._http("GET", "https://lib.example/inbox/v1/ping", "t")
    assert len(calls) == 1


def test_project_folder_is_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = load_core(tmp_path / "home", monkeypatch)
    assert core.project_from_cwd("/Users/me/Projects/Billing/web") == "billing"


def test_upload_sends_the_project_found_on_the_device(
    server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, inbox = server
    core = load_core(tmp_path / "home", monkeypatch)
    configure(core, url)
    repo = tmp_path / "anywhere" / "billing-api"
    (repo / ".git").mkdir(parents=True)
    transcript = tmp_path / ".claude" / "projects" / "-x-billing-api" / "s9.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "cwd": str(repo)}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    core.backfill(["claude-code"], days=1)  # no cwd given: read from the transcript
    assert core.flush(core.load_config())["sent"] == 1
    landed = inbox / "laptop" / "claude-code" / "projects" / "-x-billing-api" / "s9.jsonl"
    assert Path(str(landed) + ".project").read_text().strip() == "billing-api"


def test_client_home_inside_a_sandboxed_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    core = load_core(tmp_path / "explicit", monkeypatch)
    assert core.client_home() == tmp_path / "explicit"
    monkeypatch.delenv("JEZERO_CLIENT_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    agent = tmp_path / "agent-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent))
    assert core.client_home() == tmp_path / "user" / ".jezero"  # no key there: user's home
    (agent / "jezero").mkdir(parents=True)
    (agent / "jezero" / "client.json").write_text("{}")
    assert core.client_home() == agent / "jezero"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("CODEX_HOME", str(agent))
    assert core.client_home() == agent / "jezero"
