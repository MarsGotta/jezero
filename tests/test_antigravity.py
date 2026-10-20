"""Antigravity connector against a synthetic store (hand-encoded protobuf)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from jezero.connectors.antigravity import AntigravityConnector, pb_fields
from jezero.store import ConnectorState

NOW = 2_000_000_000.0
T0 = int(NOW - 7200)


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def msg(*fields: tuple[int, bytes | int | str]) -> bytes:
    out = bytearray()
    for number, value in fields:
        if isinstance(value, int):
            out += varint(number << 3) + varint(value)
        else:
            raw = value.encode() if isinstance(value, str) else value
            out += varint(number << 3 | 2) + varint(len(raw)) + raw
    return bytes(out)


def meta(seconds: int) -> bytes:
    return msg((1, msg((1, seconds), (2, 5))))


def call(name: str, **args) -> bytes:
    return msg((1, "call_1"), (2, name), (3, json.dumps(args)))


def make_store(root: Path, conv: str, steps: list[tuple[int, bytes]], workspace: str) -> None:
    (root / "conversations").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "conversations" / f"{conv}.db")
    db.execute(
        "create table steps (idx integer, step_type integer, status integer, metadata blob,"
        " step_payload blob)"
    )
    for idx, (kind, payload) in enumerate(steps):
        db.execute("insert into steps values (?,?,?,?,?)", (idx, kind, 0, meta(T0 + idx), payload))
    db.commit()
    db.close()
    os.utime(root / "conversations" / f"{conv}.db", (NOW - 7200, NOW - 7200))
    s = sqlite3.connect(root / "conversation_summaries.db")
    s.execute(
        "create table if not exists conversation_summaries (conversation_id text, title text,"
        " workspace_uris text)"
    )
    s.execute(
        "insert into conversation_summaries values (?,?,?)",
        (conv, "Fix the uploader", json.dumps([f"file://{workspace}"])),
    )
    s.commit()
    s.close()


def test_wire_reader_roundtrip() -> None:
    assert pb_fields(msg((1, 150), (2, "hi"))) == [(1, 150), (2, b"hi")]


def test_dialogue_calls_and_reply(tmp_path: Path) -> None:
    prompt = "# Agent rules\n" + "rule " * 100 + "\n# Heartbeat\nYour issue is T-7"
    make_store(
        tmp_path,
        "c1",
        [
            (14, msg((19, msg((2, prompt))))),
            (15, msg((20, msg((3, "thinking aloud"), (7, call("view_file", AbsolutePath="/a")))))),
            (132, msg((140, "huge tool output " * 100))),
            (
                15,
                msg(
                    (
                        20,
                        msg(
                            (
                                7,
                                call(
                                    "run_command", CommandLine="npm test", toolSummary="Run tests"
                                ),
                            ),
                            (7, call("run_command", CommandLine="git status")),
                            (7, call("search_web", query="uploader retries")),
                            (7, call("replace_file_content", TargetFile="/w/src/up.ts")),
                        ),
                    )
                ),
            ),
            (101, msg((114, "task finished"))),
            (15, msg((20, msg((1, "Fixed the retry loop in the uploader. " * 5))))),
            (15, b"\xff\xff\xff"),  # garbage: skipped, not fatal
        ],
        "/w/projects/uploader-wt/T-7",
    )
    opts = {
        "data_dir": str(tmp_path),
        "user_prompt_markers": ["^# Heartbeat"],
        "project_aliases": {"uploader-wt": "uploader"},
    }
    conn = AntigravityConnector("agy", opts, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState(name="agy"), now=NOW)
    (doc,) = batch.docs
    assert doc.external_id == "c1#0"
    assert doc.title_hint == "Fix the uploader"
    assert doc.project_hint == "uploader"
    text = doc.text
    assert "rule rule" not in text and "Your issue is T-7" in text
    assert "thinking aloud" not in text and "huge tool output" not in text
    assert "/a" not in text and "git status" not in text and "task finished" not in text
    assert "shell (Run tests): npm test" in text
    assert "web search: uploader retries" in text and "edit: /w/src/up.ts" in text
    assert "assistant: Fixed the retry loop" in text
    assert text.splitlines()[0].startswith("[2033-05-18 01:33]")
    again = conn.collect(ConnectorState(name="agy", cursor=batch.next_cursor), now=NOW)
    assert again.docs == []
