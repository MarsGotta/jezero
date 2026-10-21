"""Hermes support end to end: the core's export, the inbox, the connector's export mode and
the Hermes plugin (tools, first-turn context, upload), against a real Jezero server."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from jezero.connectors.hermes import HermesConnector
from jezero.inbox import target_path
from jezero.store import ConnectorState

from .test_client import CORE, configure, load_core, server  # noqa: F401  (server: fixture)

ROOT = Path(__file__).resolve().parent.parent
NOW = time.time()


def make_state_db(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL,
            cwd TEXT, model TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, timestamp REAL, active INTEGER DEFAULT 1);
        """
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("h1", "cli", "Uploader retry", NOW - 7200, "/home/me/projects/atlas/api", "m"),
            ("h-cron", "cron", None, NOW - 7200, None, "m"),
        ],
    )
    calls = json.dumps([{"function": {"name": "terminal", "arguments": '{"command": "ls"}'}}])
    con.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp, active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("h1", "user", "Why does the uploader retry forever?", None, NOW - 7000, 1),
            ("h1", "assistant", "The backoff never resets.", calls, NOW - 6990, 1),
            ("h1", "tool", "huge output", None, NOW - 6980, 1),
            ("h1", "assistant", "old answer", None, NOW - 6970, 0),
            ("h-cron", "user", "scheduled", None, NOW - 7000, 1),
        ],
    )
    con.commit()
    con.close()
    return db


def test_core_exports_a_hermes_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    db = make_state_db(tmp_path / "hermes")
    path = core.export_hermes(db, "h1")
    data = json.loads(path.read_text())
    assert data["session"]["cwd"] == "/home/me/projects/atlas/api"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]  # no tool, no inactive
    assert core.export_hermes(db, "h-cron") is None  # scheduled jobs are not conversations
    assert core.export_hermes(db, "missing") is None
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    core.export_hermes(db, "h1")
    assert path.stat().st_mtime_ns == before  # unchanged session: file untouched, no re-upload
    assert core.relpath_for("hermes", path) == "sessions/h1.json"
    assert core.cwd_of(path) == "/home/me/projects/atlas/api"
    assert target_path(tmp_path / "inbox", "laptop", "hermes", "sessions/h1.json").name == "h1.json"


def test_read_only_computer_never_queues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    core = load_core(tmp_path / "jz", monkeypatch)
    assert core.init("https://lib.example", "server-box", force=False, upload=False) == 0
    assert "--read-only" in capsys.readouterr().out
    assert core.uploads_enabled(core.load_config()) is False
    transcript = tmp_path / "projects" / "-w" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    core.hook("session-end", "claude-code", {"session_id": "s", "transcript_path": str(transcript)})
    assert not list((core.SPOOL / "pending").glob("*.json"))
    assert core.backfill(["claude-code"], 30) == 0
    assert "read-only" in capsys.readouterr().out


def test_backfill_queues_recent_hermes_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "http://127.0.0.1:9")
    make_state_db(tmp_path / "hermes")
    assert core.backfill(["hermes"], 1, str(tmp_path / "hermes")) == 0
    pending = [json.loads(p.read_text()) for p in (core.SPOOL / "pending").glob("*.json")]
    assert [(p["agent"], p["session_id"], p["project"]) for p in pending] == [
        ("hermes", "h1", "atlas")
    ]


def test_connector_reads_exports_like_the_database(tmp_path: Path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    db = make_state_db(tmp_path / "hermes")
    exports = tmp_path / "inbox" / "laptop" / "hermes" / "sessions"
    exports.mkdir(parents=True)
    shutil.copy(core.export_hermes(db, "h1"), exports / "h1.json")
    (exports / "junk.json").write_text("not json")
    later = NOW + 3 * 3600

    from_export = HermesConnector(
        "agent-hermes-laptop", {"export_dir": str(exports)}, max_segment_chars=10_000
    ).collect(ConnectorState("x"), now=later)
    from_db = HermesConnector(
        "agent-hermes", {"state_db": str(db)}, max_segment_chars=10_000
    ).collect(ConnectorState("y"), now=later)

    assert [d.text for d in from_export.docs] == [d.text for d in from_db.docs]
    doc = from_export.docs[0]
    assert doc.external_id == from_db.docs[0].external_id
    assert "tool call: terminal" in doc.text and "old answer" not in doc.text
    assert doc.project_hint == from_db.docs[0].project_hint == "atlas"
    assert from_export.next_cursor is not None
    with pytest.raises(ValueError):
        HermesConnector("bad", {}, max_segment_chars=1000)


# ------------------------------------------------------------------------- the plugin


def load_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path) -> ModuleType:
    """Lay the plugin out as the build does (core next to it) and import it."""
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "core").mkdir(parents=True)
    shutil.copy(ROOT / "clients" / "hermes" / "__init__.py", plugin_dir / "__init__.py")
    shutil.copy(CORE, plugin_dir / "core" / "jezero_client.py")
    shutil.copy(ROOT / "src" / "jezero" / "projects.py", plugin_dir / "core" / "jezero_projects.py")
    (plugin_dir / "skills" / "jezero").mkdir(parents=True)
    shutil.copy(ROOT / "clients" / "skill" / "SKILL.md", plugin_dir / "skills" / "jezero")
    monkeypatch.setenv("JEZERO_CLIENT_HOME", str(home))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "jezero_client", raising=False)
    monkeypatch.delitem(sys.modules, "jezero_projects", raising=False)
    spec = importlib.util.spec_from_file_location(
        "jezero_hermes_plugin", plugin_dir / "__init__.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module._core(), "spawn_flush", lambda: None)
    return module


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, object] = {}
        self.skills: dict[str, Path] = {}

    def register_tool(self, **kwargs) -> None:
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback

    def register_skill(self, name: str, path: Path) -> None:
        self.skills[name] = path


def test_plugin_registers_and_stays_quiet_until_connected(tmp_path, monkeypatch) -> None:
    plugin = load_plugin(tmp_path, monkeypatch, tmp_path / "jz")
    ctx = FakeContext()
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(plugin.TOOLS)
    assert set(ctx.hooks) == {"pre_llm_call", "on_session_end", "on_session_finalize"}
    assert ctx.skills["jezero"].name == "SKILL.md"
    search = ctx.tools["jezero_search"]
    assert search["check_fn"]() is False  # tools hidden until `init`
    assert "error" in json.loads(search["handler"]({"query": "x"}))
    assert plugin._pre_llm_call(session_id="s", user_message="hi", is_first_turn=True) is None
    manifest = (ROOT / "clients" / "hermes" / "plugin.yaml").read_text()
    for name in plugin.TOOLS:
        assert f"- {name}" in manifest


def test_plugin_end_to_end(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    url, inbox = server
    plugin = load_plugin(tmp_path, monkeypatch, tmp_path / "jz")
    core = plugin._core()
    configure(core, url)
    ctx = FakeContext()
    plugin.register(ctx)
    assert ctx.tools["jezero_search"]["check_fn"]() is True

    found = json.loads(ctx.tools["jezero_search"]["handler"]({"query": "uploader", "limit": None}))
    assert found["results"][0]["title"] == "Uploader retry loop fixed"

    message = "[me | Slack user] the uploader retry backoff is broken again"
    context = plugin._pre_llm_call(session_id="h1", user_message=message, is_first_turn=True)
    assert "Uploader retry loop fixed" in context["context"]
    assert plugin._pre_llm_call(session_id="h1", user_message=message, is_first_turn=False) is None

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    make_state_db(tmp_path / "hermes")
    plugin._send("h1")
    assert core.flush(core.load_config())["sent"] == 1
    landed = inbox / "laptop" / "hermes" / "sessions" / "h1.json"
    assert json.loads(landed.read_text())["session"]["id"] == "h1"
    assert (landed.parent / "h1.json.project").read_text().strip() == "atlas"


def test_plugin_throttles_uploads(tmp_path, monkeypatch) -> None:
    plugin = load_plugin(tmp_path, monkeypatch, tmp_path / "jz")
    configure(plugin._core(), "http://127.0.0.1:9")
    sent: list[str] = []

    class Inline:
        def __init__(self, target, args, **_) -> None:
            self.run = lambda: target(*args)

        def start(self) -> None:
            self.run()

    monkeypatch.setattr(plugin.threading, "Thread", Inline)
    monkeypatch.setattr(plugin, "_send", sent.append)
    plugin._on_session_end(session_id="a")
    plugin._on_session_end(session_id="a")  # within 10 minutes: skipped
    plugin._on_session_finalize(session_id="a")  # closing always sends
    plugin._on_session_end(session_id="")
    assert sent == ["a", "a"]


def test_status_of_a_read_only_computer(server, tmp_path, monkeypatch, capsys) -> None:  # noqa: F811
    url, _ = server
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    cfg_file = core.HOME / "client.json"
    cfg = json.loads(cfg_file.read_text())
    cfg["upload"] = False
    cfg_file.write_text(json.dumps(cfg))
    assert core.status() == 0
    assert "read-only" in capsys.readouterr().out
