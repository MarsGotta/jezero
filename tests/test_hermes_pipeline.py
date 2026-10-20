"""Hermes connector + summarizer + pipeline, against a synthetic Hermes database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jezero.config import Config, SummarizerConfig
from jezero.connectors.base import segment
from jezero.connectors.hermes import HermesConnector
from jezero.pipeline import run_connector
from jezero.store import ConnectorState, Store
from jezero.summarize import Summarizer

NOW = 2_000_000_000.0  # fixed "now" for the connector
HOUR = 3600.0


def make_hermes_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, timestamp REAL, active INTEGER DEFAULT 1);
        """
    )
    sessions = [
        ("s-chat", "slack", "Deploy notes", NOW - 10 * HOUR),
        ("s-cron", "cron", None, NOW - 10 * HOUR),
        ("s-live", "cli", None, NOW - 1 * HOUR),
    ]
    con.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)", sessions)
    calls = json.dumps([{"function": {"name": "terminal", "arguments": '{"command": "ls"}'}}])
    rows = [
        (
            "s-chat",
            "user",
            "How do we roll back? <memory-context>old stuff</memory-context>",
            None,
            NOW - 9 * HOUR,
        ),
        ("s-chat", "assistant", "Checking.", calls, NOW - 9 * HOUR + 1),
        ("s-chat", "tool", "huge tool output " * 100, None, NOW - 9 * HOUR + 2),
        ("s-chat", "assistant", "Run the downgrade.", None, NOW - 8 * HOUR),
        ("s-cron", "user", "scheduled job", None, NOW - 9 * HOUR),
        ("s-live", "user", "still talking", None, NOW - 5 * 60),
    ]
    con.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def fake_anthropic(calls: list[str], fail_first: int = 0) -> httpx.MockTransport:
    state = {"failures": fail_first}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["failures"]:
            state["failures"] -= 1
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        prompt = json.loads(request.content)["messages"][0]["content"]
        calls.append(prompt)
        return httpx.Response(
            200,
            json={
                "model": "fake-haiku",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "record_summary",
                        "input": {
                            "title": "Rollback of the deploy",
                            "summary": "Discussed how to roll back the deploy.",
                            "questions": ["How do we roll back the deploy?"],
                            "atoms": [
                                {"kind": "command", "text": "alembic downgrade -1"},
                                {"kind": "nonsense", "text": "dropped"},
                            ],
                            "project": "Billing-API",
                        },
                    }
                ],
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SUMMARIZER_KEY", "fake")
    db = tmp_path / "hermes.db"
    make_hermes_db(db)
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": {"model": "fake-haiku", "api_key_env": "TEST_SUMMARIZER_KEY"},
            "connectors": [
                {"name": "agent-hermes", "type": "hermes", "options": {"state_db": str(db)}}
            ],
        }
    )
    store = Store(config.db_path)
    store.init()
    return config, store, db


def connector(db: Path, **kw) -> HermesConnector:
    return HermesConnector("agent-hermes", {"state_db": str(db)}, max_segment_chars=10_000, **kw)


def test_collect_filters_defers_and_cleans(env) -> None:
    _, _, db = env
    batch = connector(db).collect(ConnectorState("agent-hermes"), now=NOW)
    assert [d.external_id.split("#")[0] for d in batch.docs] == ["s-chat"]
    assert batch.deferred == 1  # s-live is still active
    text = batch.docs[0].text
    assert "old stuff" not in text and "memory-context" not in text
    assert "huge tool output" not in text
    assert 'tool call: terminal {"command": "ls"}' in text
    assert batch.next_cursor is not None
    again = connector(db).collect(ConnectorState("agent-hermes", cursor=batch.next_cursor), now=NOW)
    assert again.docs == []


def test_segments_are_stable_when_appending() -> None:
    blocks = [(i, float(i), "x" * 40) for i in range(10)]
    first = segment(blocks, 100)
    grown = segment([*blocks, (10, 10.0, "y" * 40)], 100)
    assert grown[: len(first) - 1] == first[:-1]
    assert all(sum(len(b[2]) for b in s) <= 100 for s in first)


def run(config, store, **kw):
    conn = config.connectors[0]
    return asyncio.run(run_connector(config, store, conn, **kw))


def test_pipeline_end_to_end(env, monkeypatch) -> None:
    config, store, _ = env
    import jezero.connectors.hermes as hermes_mod

    monkeypatch.setattr(hermes_mod.time, "time", lambda: NOW)
    calls: list[str] = []
    summarizer = Summarizer(
        SummarizerConfig(model="fake-haiku", api_key_env="TEST_SUMMARIZER_KEY"),
        transport=fake_anthropic(calls, fail_first=1),
    )
    monkeypatch.setattr("jezero.summarize.asyncio.sleep", _no_sleep)

    dry = run(config, store, dry_run=True)
    assert (dry.docs, dry.stored, calls) == (1, 0, [])

    report = run(config, store, summarizer=summarizer)
    assert (report.docs, report.stored, report.failed) == (1, 1, 0)
    assert len(calls) == 1 and "<document>" in calls[0]

    hit = store.search("downgrade")[0].item
    assert hit.project == "billing-api"
    assert [a.kind for a in hit.atoms] == ["command"]  # unknown kinds are dropped
    assert "huge tool output" not in (hit.original or "")
    assert store.get_state("agent-hermes").cursor is not None

    # Nothing new: no LLM call, nothing stored.
    again = run(config, store, summarizer=summarizer)
    assert (again.docs, again.stored, len(calls)) == (0, 0, 1)


async def _no_sleep(_: float) -> None:
    return None


def test_summarizer_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    from jezero.summarize import SummarizerError

    with pytest.raises(SummarizerError, match="MISSING_KEY"):
        Summarizer(SummarizerConfig(model="m", api_key_env="MISSING_KEY"))


def test_parse_tolerates_malformed_atoms() -> None:
    from jezero.summarize import _parse

    out = _parse(
        {
            "title": "t",
            "summary": "s",
            "questions": '["q1", "q2"]',
            "atoms": [
                "fix: restart the worker",
                "plain sentence",
                {"kind": "error", "text": "E1"},
                42,
            ],
            "project": None,
        }
    )
    assert [(a.kind, a.text) for a in out.atoms] == [
        ("fix", "restart the worker"),
        ("fact", "plain sentence"),
        ("error", "E1"),
    ]
    assert out.questions == ["q1", "q2"]
    flat = _parse({"title": "t", "summary": "s", "atoms": '["fact: a"]', "project": None})
    assert [(a.kind, a.text) for a in flat.atoms] == [("fact", "a")]


def test_resummarize_rewrites_summary_and_keeps_identity(env, monkeypatch) -> None:
    from jezero.models import NewItem
    from jezero.pipeline import resummarize_items

    config, store, _ = env
    old = NewItem(
        source="agent-hermes",
        external_id="s-old#0",
        occurred_at=datetime(2033, 1, 1, tzinfo=UTC),
        title="Viejo",
        summary="Resumen viejo",
        questions=[],
        atoms=[],
        project="keep-me",
        original="user: how do we roll back the deploy?",
        original_ref="hermes-session:s-old",
        content_hash="h1",
    )
    store.upsert_many([old])
    item_id = store.search("Viejo")[0].item.id
    calls: list[str] = []
    summarizer = Summarizer(
        SummarizerConfig(model="fake-haiku", api_key_env="TEST_SUMMARIZER_KEY"),
        transport=fake_anthropic(calls),
    )
    report = asyncio.run(resummarize_items(config, store, [item_id, 9999], summarizer=summarizer))
    assert (report.docs, report.stored, report.failed) == (1, 1, 0)
    assert "roll back the deploy" in calls[0]
    fresh = store.get(item_id)
    assert fresh is not None
    assert fresh.title == "Rollback of the deploy"
    assert (fresh.external_id, fresh.project, fresh.content_hash) == ("s-old#0", "keep-me", "h1")
