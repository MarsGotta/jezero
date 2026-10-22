"""`jezero device list/revoke/rotate` and `jezero connector list/reset` on a temp config."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from jezero.cli import main as cli_main
from jezero.config import ConnectorConfig, load_config
from jezero.connectors import CONNECTOR_TYPES, ConnectorType
from jezero.connectors.base import Batch, RawDoc
from jezero.store import ConnectorState, Store

from .conftest import NOW, make_item

BASE = """\
# My library. This comment and every other byte must survive the commands.
[storage]
path = "lib.db"   # next to this file

[server]
require_auth = true

[[clients]]
# The agent on this computer.
name = "agent"
token_env = "DEVCMD_AGENT_TOKEN"   # set in .env
sources = ["agent-*"]

# Connectors below.
[[connectors]]
name = "agent-local"
type = "codex"
enabled = true # runs every 15 minutes
[connectors.options]
sessions_dir = "sessions"
"""


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("DEVCMD_AGENT_TOKEN", "x")
    path = tmp_path / "jezero.toml"
    path.write_text(BASE, encoding="utf-8")
    return path


def run(cfg: Path, *args: str) -> int:
    return cli_main(["-c", str(cfg), *args])


def backups(cfg: Path) -> list[Path]:
    return sorted(cfg.parent.glob(cfg.name + ".bak-*"))


def test_device_list_table_and_json(cfg, capsys) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    assert run(cfg, "device", "approve", "viewer", digest("v"), "--read-only") == 0
    capsys.readouterr()
    assert run(cfg, "device", "list", "--json") == 0
    entries = {e["name"]: e for e in json.loads(capsys.readouterr().out)}
    assert set(entries) == {"agent", "laptop", "viewer"}
    agent, laptop, viewer = entries["agent"], entries["laptop"], entries["viewer"]
    assert (agent["kind"], agent["upload"], agent["inbox"], agent["connectors"]) == (
        "token_env",
        False,
        None,
        [],
    )
    assert laptop["kind"] == "device" and laptop["upload"] is True
    assert laptop["inbox"] == str(cfg.parent / "data" / "inbox" / "laptop")
    assert laptop["sources"] == ["agent-*"]  # already covers agent-*-laptop
    assert {c["name"] for c in laptop["connectors"]} == {
        f"agent-{a}-laptop" for a in ("claude-code", "codex", "opencode", "hermes", "antigravity")
    }
    assert not any(c["enabled"] for c in laptop["connectors"])
    assert (viewer["upload"], viewer["inbox"], viewer["connectors"]) == (False, None, [])

    assert run(cfg, "device", "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["NAME", "KIND", "UPLOAD", "SOURCES", "INBOX"]
    assert out[1].split()[:3] == ["agent", "token_env", "no"]
    assert any(
        line.startswith("laptop connectors: agent-claude-code-laptop (disabled)") for line in out
    )


def test_revoke_with_remove_connectors_restores_the_file_byte_for_byte(cfg, capsys) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    before = cfg.read_text(encoding="utf-8")
    assert run(cfg, "device", "revoke", "laptop", "--remove-connectors") == 0
    assert cfg.read_text(encoding="utf-8") == BASE
    (backup,) = backups(cfg)
    assert backup.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "revoked laptop" in out and "removed its connectors" in out
    assert "items already stored are kept" in out
    assert (cfg.parent / "data" / "inbox" / "laptop").is_dir()  # uploads stay on disk


def test_revoke_disables_connectors_and_keeps_everything_else(cfg, capsys) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    assert run(cfg, "device", "approve", "desk", digest("d")) == 0
    text = cfg.read_text(encoding="utf-8")
    # The owner enabled one of the laptop's connectors after its pilot.
    marker = 'name = "agent-codex-laptop"\ntype = "codex"\nenabled = false'
    assert marker in text
    text = text.replace(marker, marker.replace("enabled = false", "enabled = true  # piloted"))
    cfg.write_text(text, encoding="utf-8")
    store = Store(cfg.parent / "lib.db")
    store.init()
    store.upsert(make_item("s1", "Uploaded session", "x", source="agent-codex-laptop"))
    capsys.readouterr()

    assert run(cfg, "device", "revoke", "laptop") == 0
    out = capsys.readouterr().out
    assert "disabled its connectors" in out and "items already stored are kept: 1" in out
    config = load_config(cfg)
    assert [c.name for c in config.clients] == ["agent", "desk"]
    laptop = [c for c in config.connectors if c.name.endswith("-laptop")]
    assert len(laptop) == 5 and not any(c.enabled for c in laptop)
    after = cfg.read_text(encoding="utf-8")
    assert "enabled = false  # piloted" in after  # the line's comment survives
    assert "# Device laptop" not in after and "# Device desk" in after
    # Removing the laptop's client block is the only other change.
    expected = text.replace("enabled = true  # piloted", "enabled = false  # piloted")
    start = expected.index("\n[[clients]]\n# Device laptop")
    end = expected.index("\n\n", expected.index("upload = true", start)) + 1
    assert after == expected[:start] + expected[end:]
    assert store.timeline(limit=5)[0].source == "agent-codex-laptop"


def test_revoke_refuses_unknown_names(cfg, capsys) -> None:
    assert run(cfg, "device", "revoke", "ghost") == 2
    assert "no client named 'ghost'" in capsys.readouterr().err
    assert cfg.read_text(encoding="utf-8") == BASE and backups(cfg) == []


def test_revoke_a_token_env_client(cfg) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    assert run(cfg, "device", "revoke", "agent") == 0
    after = cfg.read_text(encoding="utf-8")
    assert "DEVCMD_AGENT_TOKEN" not in after and "# The agent on this computer." not in after
    assert "# Connectors below." in after  # the next block's comment stays
    assert [c.name for c in load_config(cfg).clients] == ["laptop"]


def test_rotate_replaces_only_the_digest(cfg, capsys) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("old")) == 0
    before = cfg.read_text(encoding="utf-8")
    assert run(cfg, "device", "rotate", "laptop", f"sha256:{digest('new')}") == 0
    assert "the old key stops working within a second" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == before.replace(digest("old"), digest("new"))
    assert load_config(cfg).clients[1].token_sha256 == digest("new")
    assert len(backups(cfg)) == 1


def test_rotate_refusals_leave_the_file_alone(cfg, capsys) -> None:
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    assert run(cfg, "device", "approve", "desk", digest("d")) == 0
    before = cfg.read_text(encoding="utf-8")
    assert run(cfg, "device", "rotate", "laptop", "nothex") == 2
    assert run(cfg, "device", "rotate", "ghost", digest("n")) == 2
    assert run(cfg, "device", "rotate", "agent", digest("n")) == 1  # token_env client
    assert "change that variable instead" in capsys.readouterr().err
    # The desk's key: validation refuses two clients with one key, nothing is written.
    assert run(cfg, "device", "rotate", "laptop", digest("d")) == 1
    assert "share the same token_sha256" in capsys.readouterr().err
    assert cfg.read_text(encoding="utf-8") == before and backups(cfg) == []
    assert not list(cfg.parent.glob(".jezero-edit-*"))


def test_connector_list_shows_state_and_items(cfg, capsys) -> None:
    store = Store(cfg.parent / "lib.db")
    store.init()
    store.set_state(
        ConnectorState(
            name="agent-local",
            cursor="123.0",
            last_run_at=NOW.isoformat(),
            last_success_at=NOW.isoformat(),
            last_error="Boom: the source moved",
        )
    )
    store.upsert(make_item("a", "One", "x", source="agent-local"))
    store.upsert(make_item("b", "Two", "y", source="agent-local"))
    assert run(cfg, "device", "approve", "laptop", digest("l")) == 0
    capsys.readouterr()
    assert run(cfg, "connector", "list", "--json") == 0
    entries = {e["name"]: e for e in json.loads(capsys.readouterr().out)}
    local = entries["agent-local"]
    assert (local["type"], local["enabled"], local["interval_minutes"], local["items"]) == (
        "codex",
        True,
        15,
        2,
    )
    assert local["last_error"] == "Boom: the source moved"
    assert local["last_run_at"] == NOW.isoformat()
    assert entries["agent-codex-laptop"]["enabled"] is False
    assert entries["agent-codex-laptop"]["last_run_at"] is None
    assert run(cfg, "connector", "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["NAME", "TYPE", "ENABLED", "EVERY"]
    assert lines[1].split()[:5] == ["agent-local", "codex", "yes", "15m", "2026-10-20"]


def test_connector_list_without_a_database(cfg, capsys) -> None:
    assert run(cfg, "connector", "list", "--json") == 0
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry["items"] == 0 and entry["last_run_at"] is None
    assert not (cfg.parent / "lib.db").exists()


def test_connector_reset(cfg, capsys, monkeypatch) -> None:
    store = Store(cfg.parent / "lib.db")
    store.init()
    state = ConnectorState(
        name="agent-local", cursor="99.5", last_run_at="r", last_success_at="s", last_item_at="i"
    )
    store.set_state(state)
    assert run(cfg, "connector", "reset", "ghost", "--yes") == 2
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run(cfg, "connector", "reset", "agent-local") == 2  # no terminal, no --yes
    assert store.get_state("agent-local").cursor == "99.5"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert run(cfg, "connector", "reset", "agent-local") == 1
    assert store.get_state("agent-local").cursor == "99.5"
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    capsys.readouterr()
    assert run(cfg, "connector", "reset", "agent-local") == 0
    assert "skipped by content hash" in capsys.readouterr().out
    assert store.get_state("agent-local") == ConnectorState(
        name="agent-local", cursor=None, last_run_at="r", last_success_at="s", last_item_at="i"
    )
    assert run(cfg, "connector", "reset", "agent-local", "--yes") == 0
    assert "has no cursor yet" in capsys.readouterr().out


class _Docs:
    """A connector over a fixed list of docs that honours its cursor (doc index)."""

    def __init__(self, name: str, texts: list[str]) -> None:
        self.name, self.texts = name, texts

    def collect(self, state, *, limit=None, sample=None) -> Batch:
        start = int(state.cursor or 0)
        docs = [
            RawDoc(external_id=f"d{i}", occurred_at=NOW, text=text)
            for i, text in enumerate(self.texts)
            if i >= start
        ]
        return Batch(docs=docs, next_cursor=str(len(self.texts)))


class _Summary:
    title, summary, questions, atoms, project = "T", "S", [], [], None


class _FakeSummarizer:
    def __init__(self) -> None:
        self.calls = 0
        self.usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()

    async def summarize(self, text, **_):
        self.calls += 1
        return _Summary()


def test_reset_rereads_but_unchanged_units_cost_nothing(tmp_path, monkeypatch) -> None:
    """What `connector reset` promises: the source is read again, stored items whose
    redacted text did not change are skipped by content hash, changed ones are redone."""
    from jezero.config import Config
    from jezero.pipeline import run_connector

    texts = ["first session text", "second session text"]
    kind = ConnectorType("fakedocs", "available", "test", build=lambda c, ctx: _Docs(c.name, texts))
    monkeypatch.setitem(CONNECTOR_TYPES, "fakedocs", kind)
    conn = ConnectorConfig.model_validate({"name": "agent-fake", "type": "fakedocs"})
    config = Config(summarizer={"model": "m"}, connectors=[conn])
    store = Store(tmp_path / "lib.db")
    store.init()
    first = _FakeSummarizer()
    asyncio.run(run_connector(config, store, conn, summarizer=first))
    assert first.calls == 2 and store.get_state("agent-fake").cursor == "2"

    assert store.reset_cursor("agent-fake")
    texts[1] = "second session text, grown"
    again = _FakeSummarizer()
    report = asyncio.run(run_connector(config, store, conn, summarizer=again))
    assert (report.docs, report.unchanged, again.calls) == (2, 1, 1)
    assert len(store.timeline(limit=10)) == 2
