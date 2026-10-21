"""OpenCode connector against a synthetic opencode.db."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from jezero.connectors.opencode import OpenCodeConnector
from jezero.store import ConnectorState

NOW = 2_000_000_000.0
OLD_MS = (NOW - 7200) * 1000


def make_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        """
        create table session (id text primary key, project_id text, parent_id text,
          directory text, title text, time_created integer, time_updated integer);
        create table message (id text primary key, session_id text, time_created integer,
          time_updated integer, data text);
        create table part (id text primary key, message_id text, session_id text,
          time_created integer, time_updated integer, data text);
        create table event (id text, data text);
        """
    )
    return db


def add_session(db, sid, directory, title, parts, *, parent=None, updated=OLD_MS):
    db.execute(
        "insert into session values (?,?,?,?,?,?,?)",
        (sid, "p", parent, directory, title, OLD_MS - 1000, updated),
    )
    for n, (role, part) in enumerate(parts):
        mid = f"{sid}-m{n}"
        info = {"role": role, "agent": "build", "modelID": "m-1"}
        db.execute(
            "insert into message values (?,?,?,?,?)", (mid, sid, OLD_MS + n, 0, json.dumps(info))
        )
        db.execute(
            "insert into part values (?,?,?,?,?,?)",
            (f"{sid}-p{n}", mid, sid, OLD_MS + n, 0, json.dumps(part)),
        )
    db.commit()


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool(name: str, **inp) -> dict:
    return {
        "type": "tool",
        "tool": name,
        "state": {"status": "completed", "input": inp, "output": "X" * 5000},
    }


def test_dialogue_with_changes_only(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    db = make_db(path)
    add_session(
        db,
        "s1",
        "/w/projects/shop-wt/T-1",
        "Fix checkout",
        [
            (
                "user",
                text("# Agent rules\n" + "rule " * 100 + "\n## Wake Payload\nFix the checkout bug"),
            ),
            ("assistant", {"type": "reasoning", "text": "secret thoughts"}),
            ("assistant", tool("read", filePath="a.py")),
            ("assistant", tool("bash", command="git status")),
            ("assistant", tool("bash", command="npm test -- checkout")),
            ("assistant", tool("edit", filePath="src/checkout.ts", oldString="a", newString="b")),
            ("assistant", tool("todowrite", todos=[])),
            ("assistant", {"type": "step-finish", "tokens": {}}),
            ("assistant", text("Fixed the rounding error in checkout. " * 10)),
        ],
    )
    add_session(db, "s2", "/tmp/opencode-test", "probe", [("user", text("hola " * 200))])
    add_session(db, "s3", "/w/projects/shop", "child", [("user", text("x " * 400))], parent="s1")
    add_session(
        db, "s4", "/w/projects/shop", "live", [("user", text("y " * 400))], updated=NOW * 1000
    )
    opts = {
        "db_path": str(path),
        "skip_dir_globs": ["/tmp/*"],
        "user_prompt_markers": ["^## Wake Payload"],
        "project_aliases": {"shop-wt": "shop"},
    }
    conn = OpenCodeConnector("oc", opts, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState(name="oc"), now=NOW)
    (doc,) = batch.docs
    assert batch.deferred == 1
    assert doc.external_id == "s1#0"
    assert doc.title_hint == "Fix checkout"
    assert doc.project_hint == "shop"
    assert doc.original_ref == "opencode-session:s1"
    assert "rule rule" not in doc.text and "Fix the checkout bug" in doc.text
    assert "secret thoughts" not in doc.text and "XXXX" not in doc.text
    assert "a.py" not in doc.text and "git status" not in doc.text and "todowrite" not in doc.text
    assert "shell: npm test -- checkout" in doc.text
    assert "edit: src/checkout.ts" in doc.text
    assert "assistant: Fixed the rounding error" in doc.text
    again = conn.collect(ConnectorState(name="oc", cursor=batch.next_cursor), now=NOW)
    assert again.docs == []


def test_plugin_exports_are_read_like_the_database(tmp_path: Path) -> None:
    import json
    import os

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    created = 1_900_000_000_000
    export = {
        "session": {"id": "ses_1", "directory": "/w/projects/uploader", "title": "Fix uploader"},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": created}},
                "parts": [{"id": "p1", "type": "text", "text": "Make the uploader retry " * 30}],
            },
            {
                "info": {"id": "m2", "role": "assistant", "time": {"created": created + 5000}},
                "parts": [
                    {"id": "p2", "type": "reasoning", "text": "secret thinking"},
                    {"id": "p3", "type": "text", "text": "Done: retries use backoff."},
                ],
            },
        ],
    }
    path = export_dir / "ses_1.json"
    path.write_text(json.dumps(export))
    os.utime(path, (created / 1000, created / 1000))
    from jezero.connectors.opencode import OpenCodeConnector
    from jezero.store import ConnectorState

    conn = OpenCodeConnector("o", {"export_dir": str(export_dir)}, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState("o"), now=created / 1000 + 7200)
    (doc,) = batch.docs
    assert doc.project_hint == "uploader" and doc.title_hint == "Fix uploader"
    assert "Make the uploader retry" in doc.text and "Done: retries use backoff." in doc.text
    assert "secret thinking" not in doc.text
