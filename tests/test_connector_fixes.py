"""Regression tests for the connectors: missing folders, hermes min_chars, paperclip
aliases and whole-session marking."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jezero import cli
from jezero.config import Config
from jezero.connectors.antigravity import AntigravityConnector
from jezero.connectors.claude_code import ClaudeCodeConnector
from jezero.connectors.codex import CodexConnector
from jezero.connectors.hermes import HermesConnector
from jezero.connectors.opencode import OpenCodeConnector
from jezero.connectors.paperclip import PaperclipConnector
from jezero.projects import ProjectRules
from jezero.store import ConnectorState, Store
from jezero.watchdog import evaluate

from .test_paperclip import api, issue

NOW = 2_000_000_000.0
HOUR = 3600.0
STATE = ConnectorState("x")


# -- bug 5: a configured folder that does not exist is an error everywhere ---------------


@pytest.mark.parametrize(
    ("kind", "option"),
    [
        (ClaudeCodeConnector, "projects_dir"),
        (CodexConnector, "sessions_dir"),
        (OpenCodeConnector, "export_dir"),
        (OpenCodeConnector, "db_path"),
        (HermesConnector, "export_dir"),
        (HermesConnector, "state_db"),
        (AntigravityConnector, "transcripts_dir"),
        (AntigravityConnector, "data_dir"),
    ],
)
def test_missing_source_is_an_error_naming_the_option(tmp_path: Path, kind, option) -> None:
    missing = tmp_path / "gone"
    conn = kind("c", {option: str(missing)}, max_segment_chars=10_000)
    with pytest.raises(FileNotFoundError, match=f"{option} (folder|file) not found: .*gone"):
        conn.collect(STATE, now=NOW)


def test_missing_export_dir_shows_as_failing_in_health(tmp_path: Path, monkeypatch) -> None:
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "connectors": [
                {
                    "name": "agent-hermes-laptop",
                    "type": "hermes",
                    "options": {"export_dir": str(tmp_path / "inbox" / "missing")},
                }
            ],
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["run"]) == 1
    (entry,) = evaluate(config, Store(config.db_path))
    assert entry.status == "failing"
    assert "export_dir folder not found" in (entry.last_error or "")
    assert str(tmp_path / "inbox" / "missing") in (entry.last_error or "")


# -- bug 6: hermes skips trivial sessions (`min_chars`, default 400) ----------------------


def hermes_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, timestamp REAL);
        """
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)",
        [("s-hi", "cli", None, NOW - 9 * HOUR), ("s-work", "cli", None, NOW - 10 * HOUR)],
    )
    con.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [
            ("s-work", "user", "Migrate the queue worker " + "detail " * 60, NOW - 10 * HOUR),
            ("s-work", "assistant", "Done, the worker now uses the new queue.", NOW - 10 * HOUR),
            ("s-hi", "user", "hi", NOW - 8 * HOUR),  # newest, and trivial
            ("s-hi", "assistant", "Hello!", NOW - 8 * HOUR),
        ],
    )
    con.commit()
    con.close()
    return path


def test_hermes_min_chars_skips_trivial_sessions(tmp_path: Path) -> None:
    db = hermes_db(tmp_path / "state.db")
    default = HermesConnector("h", {"state_db": str(db)}, max_segment_chars=10_000)
    batch = default.collect(STATE, now=NOW)
    assert [d.external_id.split("#")[0] for d in batch.docs] == ["s-work"]
    assert batch.next_cursor == repr(NOW - 8 * HOUR)  # the trivial one is still consumed
    # A sample of one skips the trivial newest session and shows the real one.
    (doc,) = default.collect(STATE, sample=1, now=NOW).docs
    assert doc.external_id.startswith("s-work#")
    everything = HermesConnector(
        "h", {"state_db": str(db), "min_chars": 0}, max_segment_chars=10_000
    )
    assert len(everything.collect(STATE, now=NOW).docs) == 2


def test_hermes_exports_apply_min_chars(tmp_path: Path) -> None:
    exports = tmp_path / "sessions"
    exports.mkdir()
    message = {"id": 1, "role": "user", "content": "hi", "timestamp": NOW - 9 * HOUR}
    (exports / "s-hi.json").write_text(
        json.dumps({"session": {"source": "cli"}, "messages": [message]})
    )
    conn = HermesConnector("h", {"export_dir": str(exports)}, max_segment_chars=10_000)
    batch = conn.collect(STATE, now=NOW + 365 * 24 * HOUR)
    assert batch.docs == [] and batch.next_cursor is not None


# -- bug 7: paperclip applies the project aliases -----------------------------------------


def test_paperclip_project_map_goes_through_aliases(monkeypatch) -> None:
    monkeypatch.setenv("PC_KEY", "k")
    transport, _ = api([issue("i1", "ACME-1")], {})
    options = {
        "api_url": "http://pc.invalid/api",
        "api_key_env": "PC_KEY",
        "project_map": {"Shop app": "Shop-Old"},
    }
    rules = ProjectRules(aliases={"shop-old": "shop"})
    conn = PaperclipConnector(
        "pc", options, max_segment_chars=60_000, project_rules=rules, transport=transport
    )
    (doc,) = conn.collect(STATE, now=NOW).docs
    assert doc.project_hint == "shop"
    own = PaperclipConnector(
        "pc",
        {**options, "project_aliases": {"Shop-Old": "storefront"}},
        max_segment_chars=60_000,
        project_rules=rules,
        transport=transport,
    )
    assert own.collect(STATE, now=NOW).docs[0].project_hint == "storefront"
    plain = PaperclipConnector("pc", options, max_segment_chars=60_000, transport=transport)
    assert plain.collect(STATE, now=NOW).docs[0].project_hint == "Shop-Old"


def test_paperclip_build_passes_the_project_rules(tmp_path: Path) -> None:
    from jezero.pipeline import build_connector

    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "projects": {"aliases": {"shop-old": "shop"}},
            "connectors": [{"name": "pc", "type": "paperclip", "options": {}}],
        }
    )
    conn = build_connector(config, config.connectors[0])
    assert conn.rules.aliases == {"shop-old": "shop"}


# -- bug 9 (connector side): every segment of a session is marked as complete -------------


def test_connectors_mark_whole_sessions(tmp_path: Path) -> None:
    db = hermes_db(tmp_path / "state.db")
    conn = HermesConnector("h", {"state_db": str(db)}, max_segment_chars=200)
    docs = conn.collect(STATE, now=NOW).docs
    assert len(docs) > 1
    ids = tuple(d.external_id for d in docs)
    assert all(d.session == "s-work" and d.session_segments == ids for d in docs)
    assert all("session" not in d.meta for d in docs)  # nothing extra reaches the prompt
