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


def test_unreadable_format_is_a_warning_that_holds_the_cursor(tmp_path: Path) -> None:
    good = [(14, msg((19, msg((2, "Fix the uploader " * 40))))), (15, msg((20, msg((1, "ok")))))]
    make_store(tmp_path, "good", good, "/w/projects/up")
    # A new format: same step types, fields moved elsewhere.
    changed = [(14, msg((44, msg((9, "hello " * 80))))), (15, msg((55, msg((9, "done")))))]
    make_store(tmp_path, "changed", changed, "/w/projects/up")
    make_store(tmp_path, "empty", [], "/w/projects/up")
    conn = AntigravityConnector("agy", {"data_dir": str(tmp_path)}, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState(name="agy"), now=NOW)
    assert [d.external_id for d in batch.docs] == ["good#0"]
    (warning,) = batch.warnings
    assert "1 conversation(s)" in warning and "changed" in warning


def test_pipeline_turns_the_warning_into_a_failing_connector(tmp_path: Path) -> None:
    import asyncio

    from jezero.config import Config
    from jezero.pipeline import run_connector
    from jezero.store import Store
    from jezero.watchdog import evaluate

    changed = [(14, msg((44, msg((9, "hello " * 80))))), (15, msg((55, msg((9, "done")))))]
    make_store(tmp_path / "agy", "changed", changed, "/w/projects/up")
    import time

    past = time.time() - 7200  # run_connector uses the real clock
    os.utime(tmp_path / "agy" / "conversations" / "changed.db", (past, past))
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": {"model": "m", "api_key_env": "UNUSED_KEY"},
            "connectors": [
                {
                    "name": "agy",
                    "type": "antigravity",
                    "options": {"data_dir": str(tmp_path / "agy")},
                }
            ],
        }
    )
    store = Store(config.db_path)
    store.init()

    class NoCalls:
        usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()

        async def summarize(self, *a, **k):  # pragma: no cover - nothing to summarise
            raise AssertionError("no docs expected")

    report = asyncio.run(run_connector(config, store, config.connectors[0], summarizer=NoCalls()))
    assert report.warnings and report.stored == 0
    state = store.get_state("agy")
    assert state.cursor is None and "format check" in (state.last_error or "")
    (health,) = evaluate(config, store)
    assert health.status == "failing"
