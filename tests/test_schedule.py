"""Scheduling helpers: due connectors, failure isolation, run lock, watchdog."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from jezero import cli
from jezero.config import Config
from jezero.store import ConnectorState, Store
from jezero.watchdog import alert, evaluate, heartbeat

NOW = datetime(2033, 5, 18, 12, 0, tzinfo=UTC)


def make(tmp_path, **watchdog) -> tuple[Config, Store]:
    config = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "connectors": [
                {"name": "fresh", "type": "hermes", "options": {"state_db": "/missing.db"}},
                {"name": "old", "type": "hermes", "options": {"state_db": "/missing.db"}},
                {"name": "new", "type": "hermes", "options": {"state_db": "/missing.db"}},
                {
                    "name": "off",
                    "type": "hermes",
                    "enabled": False,
                    "options": {"state_db": "/x"},
                },
            ],
            "watchdog": {"max_silence_hours": 24, **watchdog},
        }
    )
    store = Store(config.db_path)
    store.init()
    store.set_state(ConnectorState("fresh", last_success_at=(NOW - timedelta(hours=1)).isoformat()))
    store.set_state(ConnectorState("old", last_success_at=(NOW - timedelta(hours=30)).isoformat()))
    return config, store


def test_evaluate_statuses(tmp_path) -> None:
    config, store = make(tmp_path)
    report = {e.connector: e.status for e in evaluate(config, store, now=NOW)}
    assert report == {"fresh": "ok", "old": "stale", "new": "never"}


def test_alerts_are_deduplicated(tmp_path, monkeypatch) -> None:
    config, store = make(tmp_path, alert_webhook_env="TEST_HOOK", realert_hours=12)
    monkeypatch.setenv("TEST_HOOK", "https://hooks.example.invalid/x")
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    report = evaluate(config, store, now=NOW)
    assert sorted(alert(config, store, report, now=NOW, transport=transport)) == ["new", "old"]
    assert "old is stale" in posted[0]["text"] or "old is stale" in posted[1]["text"]
    later = NOW + timedelta(hours=1)
    assert alert(config, store, report, now=later, transport=transport) == []
    much_later = NOW + timedelta(hours=13)
    assert len(alert(config, store, report, now=much_later, transport=transport)) == 2


def test_run_isolates_failures_and_respects_due(tmp_path, monkeypatch, capsys) -> None:
    config, store = make(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["run"]) == 1  # every source is missing, but each one is tried
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["connector"] for x in lines] == ["fresh", "old", "new"]
    assert "not found" in store.get_state("old").last_error
    # Just ran: nothing is due, and one line says so and when each one runs next.
    assert cli.main(["run", "--due"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    summary = json.loads(line)["due"]
    assert summary["ran"] == []
    assert sorted(summary["not_due_until"]) == ["fresh", "new", "old"]
    next_run = datetime.fromisoformat(summary["not_due_until"]["old"])
    assert timedelta(minutes=14) < next_run - datetime.now(UTC) <= timedelta(minutes=15)


def test_heartbeat_reports_overall_status(tmp_path, monkeypatch) -> None:
    config, store = make(tmp_path, heartbeat_url_env="TEST_PUSH")
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
    report = evaluate(config, store, now=NOW)
    assert heartbeat(config, report, transport=transport) is None  # env var not set
    monkeypatch.setenv("TEST_PUSH", "http://push.example.invalid/api/push/abc")
    assert heartbeat(config, report, transport=transport) == "down"
    assert seen[-1].url.params["status"] == "down"
    assert "old stale" in seen[-1].url.params["msg"]
    healthy = [e for e in report if e.healthy]
    assert heartbeat(config, healthy, transport=transport) == "up"
