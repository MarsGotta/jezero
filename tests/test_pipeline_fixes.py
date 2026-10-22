"""Regression tests for the pipeline, `run`/`health` commands, watchdog and summarizer."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jezero import cli
from jezero import summarize as summarize_module
from jezero.config import Config
from jezero.models import NewItem
from jezero.pipeline import run_connector
from jezero.store import ConnectorState, Store
from jezero.summarize import SummarizerError, Summary, Usage
from jezero.watchdog import alert, evaluate

OLD = 1_700_000_000.0  # file times well before "now", so sessions are settled


class FakeSummarizer:
    """Stands in for the LLM: no network, records what it was asked."""

    def __init__(self, fail: bool = False) -> None:
        self.usage = Usage()
        self.calls: list[str] = []
        self.fail = fail

    async def summarize(self, text: str, *, title_hint=None, context=None) -> Summary:
        self.calls.append(text)
        if self.fail:
            raise SummarizerError("model unavailable")
        return Summary("Synthetic title", "Synthetic summary.", ["q?"], [], None)


PruningStore = Store  # the real store prunes; kept as a name for the tests below


def claude_session(root: Path, sid: str, turns: int, *, mtime: float = OLD) -> Path:
    path = root / "-w-projects-demo" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for n in range(turns):
        role = "user" if n % 2 == 0 else "assistant"
        records.append(
            {
                "type": role,
                "sessionId": sid,
                "timestamp": "2030-03-17T00:00:00Z",
                "cwd": "/w/projects/demo",
                "message": {"role": role, "content": f"turn {n} " + "word " * 60},
            }
        )
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    os.utime(path, (mtime, mtime))
    return path


def make_config(tmp_path: Path, *, max_input_chars: int = 60_000, **conn) -> Config:
    projects = tmp_path / "claude"
    projects.mkdir(exist_ok=True)
    entry = {
        "name": "agent-claude",
        "type": "claude_code",
        "options": {"projects_dir": str(projects)},
    }
    entry.update(conn)
    return Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": {"max_input_chars": max_input_chars},  # no model: unusable
            "connectors": [entry],
        }
    )


def run(config: Config, store: Store, **kw):
    return asyncio.run(run_connector(config, store, config.connectors[0], **kw))


# -- bug 1: a sample run leaves the connector state alone ------------------------------


def test_sample_does_not_touch_cursor_or_run_times(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claude_session(tmp_path / "claude", "s1", 4)
    store = Store(config.db_path)
    store.init()
    report = run(config, store, sample=1, summarizer=FakeSummarizer())
    assert (report.docs, report.stored, report.sample) == (1, 1, True)
    assert store.get_state("agent-claude") == ConnectorState("agent-claude")
    # Still due for the schedule and "never" ran for the watchdog.
    assert cli._is_due(store, config.connectors[0], datetime.now(UTC))
    assert [e.status for e in evaluate(config, store)] == ["never"]


def test_failed_sample_from_the_cli_records_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path)
    (tmp_path / "claude").rmdir()  # the source folder is gone
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["run", "--sample", "2"]) == 1
    assert "projects_dir folder not found" in capsys.readouterr().out
    store = Store(config.db_path)
    assert store.get_state("agent-claude") == ConnectorState("agent-claude")


# -- bug 2: collect first, ask for a summarizer only when there is work -----------------


def test_nothing_ready_needs_no_summarizer(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claude_session(tmp_path / "claude", "live", 4, mtime=datetime.now(UTC).timestamp())
    store = Store(config.db_path)
    store.init()
    pilot = run(config, store, sample=3)  # summarizer.model is unset: would raise
    assert (pilot.docs, pilot.deferred_sessions) == (0, 1)
    assert pilot.note == "nothing to summarise (1 still active, deferred)"
    full = run(config, store)
    assert full.note and full.failed == 0
    assert store.get_state("agent-claude").last_success_at is not None


def test_work_without_a_summarizer_still_fails_clearly(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    claude_session(tmp_path / "claude", "s1", 4)
    store = Store(config.db_path)
    store.init()
    with pytest.raises(SummarizerError, match="summarizer.model"):
        run(config, store, sample=1)


# -- bug 3: `run` always says something -----------------------------------------------


def test_run_without_enabled_connectors_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path, enabled=False)
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["run"]) == 0
    assert capsys.readouterr().out == "no enabled connectors: enable one in jezero.toml\n"
    assert cli.main(["run", "--due"]) == 0
    assert "no enabled connectors" in capsys.readouterr().out


def test_run_due_prints_one_summary_line(tmp_path: Path, monkeypatch, capsys) -> None:
    base = make_config(tmp_path)
    data = base.model_dump(mode="json")
    data["connectors"].append({**data["connectors"][0], "name": "agent-two"})
    config = Config.model_validate(data)
    store = Store(config.db_path)
    store.init()
    recent = datetime.now(UTC) - timedelta(minutes=5)
    store.set_state(ConnectorState("agent-two", last_run_at=recent.isoformat()))
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["run", "--due"]) == 0  # agent-claude: empty folder, nothing to do
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["connector"] == "agent-claude"
    summary = lines[-1]["due"]
    assert summary["ran"] == ["agent-claude"]
    due_at = datetime.fromisoformat(summary["not_due_until"]["agent-two"])
    assert abs(due_at - (recent + timedelta(minutes=15))) < timedelta(seconds=1)


# -- bug 4: an unreachable alert webhook is logged, not fatal --------------------------


def test_alert_network_error_is_caught(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path, options={"projects_dir": "/missing"})
    config = Config.model_validate(
        {**config.model_dump(mode="json"), "watchdog": {"alert_webhook_env": "TEST_HOOK"}}
    )
    monkeypatch.setenv("TEST_HOOK", "https://hooks.example.invalid/x")
    store = Store(config.db_path)
    store.init()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    errors: list[str] = []
    report = evaluate(config, store)
    sent = alert(config, store, report, transport=httpx.MockTransport(refuse), errors=errors)
    assert sent == [] and errors == ["alert webhook failed: ConnectError"]
    assert store.get_meta("watchdog.last_alert.agent-claude") is None  # retried next time


def test_health_survives_webhook_and_heartbeat_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path)
    config = Config.model_validate(
        {
            **config.model_dump(mode="json"),
            "watchdog": {"alert_webhook_env": "TEST_HOOK", "heartbeat_url_env": "TEST_PUSH"},
        }
    )
    sent: list[str] = []

    def post(self, url, **kw):
        raise httpx.ConnectError("refused")

    def get(self, url, **kw):
        sent.append(url)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setenv("TEST_HOOK", "https://hooks.example.invalid/x")
    monkeypatch.setenv("TEST_PUSH", "https://push.example.invalid/x")
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    assert cli.main(["health"]) == 1  # the connector never ran: still reported as unhealthy
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert {"alert_error": "alert webhook failed: ConnectError"} in out
    assert {"heartbeat_error": "ConnectError"} in out
    assert sent == ["https://push.example.invalid/x"]  # the heartbeat was still tried


def test_never_succeeded_with_an_error_is_failing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = Store(config.db_path)
    store.init()
    store.set_state(ConnectorState("agent-claude", last_error="export_dir folder not found: /x"))
    (entry,) = evaluate(config, store)
    assert (entry.status, entry.last_error) == ("failing", "export_dir folder not found: /x")


# -- bug 8: the prompt states the caps the parser enforces ------------------------------


def test_prompt_and_parser_agree_on_caps() -> None:
    prompt = summarize_module.SYSTEM_PROMPT
    assert f"up to {summarize_module.MAX_ATOMS} " in prompt
    assert f"3 to {summarize_module.MAX_QUESTIONS} " in prompt
    assert f"past {summarize_module.MAX_TITLE_CHARS} is cut" in prompt
    parsed = summarize_module._parse(
        {
            "title": "t" * 300,
            "summary": "s",
            "questions": ["q"] * 10,
            "atoms": [{"kind": "fact", "text": "a"}] * 20,
        }
    )
    assert len(parsed.title) == summarize_module.MAX_TITLE_CHARS
    assert len(parsed.questions) == summarize_module.MAX_QUESTIONS
    assert len(parsed.atoms) == summarize_module.MAX_ATOMS


# -- bug 9: stale segments of a session read again are pruned ---------------------------


def _ids(store: Store) -> list[str]:
    with store._read() as con:
        return sorted(r[0] for r in con.execute("SELECT external_id FROM items"))


def test_resegmented_session_drops_old_parts(tmp_path: Path) -> None:
    small = make_config(tmp_path, max_input_chars=1_000)
    path = claude_session(tmp_path / "claude", "s1", 12)
    store = PruningStore(small.db_path)
    store.init()
    other = NewItem(
        source="agent-claude",
        external_id="s10#0",  # another session whose id shares the leading characters
        occurred_at=datetime(2030, 1, 1, tzinfo=UTC),
        title="Other",
        summary="Other session.",
        questions=[],
        atoms=[],
    )
    store.upsert_many([other])
    first = run(small, store, summarizer=FakeSummarizer())
    assert first.docs > 2 and first.pruned == 0
    before = _ids(store)

    big = make_config(tmp_path, max_input_chars=60_000)
    os.utime(path, (OLD + 60, OLD + 60))  # the session is read again
    failing = run(big, store, summarizer=FakeSummarizer(fail=True))
    assert failing.pruned == 0 and _ids(store) == before  # nothing lost on failure

    again = run(big, store, summarizer=FakeSummarizer())
    assert again.docs == 1 and again.pruned == first.docs - 1
    assert _ids(store) == ["s1#0", "s10#0"]


def test_first_run_prunes_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_input_chars=1_000)
    claude_session(tmp_path / "claude", "s1", 12)
    store = Store(config.db_path)
    store.init()
    report = run(config, store, summarizer=FakeSummarizer())
    assert report.stored == report.docs > 1 and report.pruned == 0


# -- bug 10: stored items carry the connector's resolved trust -------------------------


def test_items_carry_the_connector_trust(tmp_path: Path) -> None:
    config = make_config(tmp_path, trust="external")
    claude_session(tmp_path / "claude", "s1", 4)
    store = Store(config.db_path)
    store.init()
    run(config, store, summarizer=FakeSummarizer())
    (hit,) = store.search("Synthetic")
    assert hit.item.trust == "external"


# -- the summarizer's project guess goes through the aliases ---------------------------


class GuessingSummarizer(FakeSummarizer):
    async def summarize(self, text: str, *, title_hint=None, context=None) -> Summary:
        self.calls.append(text)
        return Summary("Synthetic title", "Synthetic summary.", ["q?"], [], "Northwind")


def test_model_guess_goes_through_aliases_and_retag_fixes_old_ones(tmp_path: Path) -> None:
    from jezero.cli import main as cli_main

    path = claude_session(tmp_path / "claude", "s1", 4)
    path.write_text(path.read_text().replace('"cwd": "/w/projects/demo"', '"cwd": "/w/x"'))
    os.utime(path, (OLD, OLD))
    config = make_config(tmp_path)
    store = Store(config.db_path)
    store.init()
    run(config, store, summarizer=GuessingSummarizer())
    (hit,) = store.search("Synthetic")
    assert hit.item.project == "Northwind"  # no alias yet: the guess is kept

    aliased = make_config(
        tmp_path,
        options={
            "projects_dir": str(tmp_path / "claude"),
            "project_aliases": {"northwind": "atlas"},
        },
    )
    cfg_file = tmp_path / "jezero.toml"
    cfg_file.write_text(
        f'''[storage]
path = "{config.db_path}"

[[connectors]]
name = "agent-claude"
type = "claude_code"
[connectors.options]
projects_dir = "{tmp_path / "claude"}"
settle_minutes = 0
project_aliases = {{ "northwind" = "atlas" }}
'''
    )
    assert cli_main(["-c", str(cfg_file), "retag", "--connector", "agent-claude"]) == 0
    (hit,) = store.search("Synthetic")
    assert hit.item.project == "atlas"
    assert aliased.connectors[0].options["project_aliases"] == {"northwind": "atlas"}


def test_retag_ask_model_moves_unmentioned_guesses(tmp_path: Path, monkeypatch, capsys) -> None:
    import jezero.summarize as summarize_module
    from jezero.cli import main as cli_main

    path = claude_session(tmp_path / "claude", "s1", 4)
    path.write_text(path.read_text().replace('"cwd": "/w/projects/demo"', '"cwd": "/w/x"'))
    os.utime(path, (OLD, OLD))
    config = make_config(tmp_path)
    store = Store(config.db_path)
    store.init()
    run(config, store, summarizer=GuessingSummarizer())  # stored as "Northwind", never named

    class FakeModel:
        def __init__(self, *a, **kw) -> None:
            self.usage = Usage()

        async def guess_project(self, text: str) -> str | None:
            assert "Synthetic summary." in text  # asked from the summary, not the original
            self.usage.calls += 1
            return "synthetic" if "Synthetic" in text else "atlas"

    monkeypatch.setattr(summarize_module, "Summarizer", FakeModel)
    cfg_file = tmp_path / "jezero.toml"
    cfg_file.write_text(
        f'''[storage]
path = "{config.db_path}"

[[connectors]]
name = "agent-claude"
type = "claude_code"
[connectors.options]
projects_dir = "{tmp_path / "claude"}"
'''
    )
    log = tmp_path / "moves.jsonl"
    args = ["-c", str(cfg_file), "retag", "--connector", "agent-claude", "--ask-model"]
    assert cli_main([*args, "--dry-run"]) == 0
    (hit,) = store.search("Synthetic")
    assert hit.item.project == "Northwind"  # dry run writes nothing
    assert cli_main([*args, "--log", str(log)]) == 0
    (hit,) = store.search("Synthetic")
    assert hit.item.project == "synthetic"  # named in its own title, so it is used
    assert json.loads(log.read_text()) == {
        "id": hit.item.id,
        "old": "Northwind",
        "new": "synthetic",
    }
    assert "Northwind -> synthetic" in capsys.readouterr().err


def test_retag_ask_model_leaves_out_answers_the_text_never_names(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import jezero.summarize as summarize_module
    from jezero.cli import main as cli_main

    path = claude_session(tmp_path / "claude", "s1", 4)
    path.write_text(path.read_text().replace('"cwd": "/w/projects/demo"', '"cwd": "/w/x"'))
    os.utime(path, (OLD, OLD))
    config = make_config(tmp_path)
    store = Store(config.db_path)
    store.init()
    run(config, store, summarizer=GuessingSummarizer())

    class DriftingModel:
        def __init__(self, *a, **kw) -> None:
            self.usage = Usage()

        async def guess_project(self, text: str) -> str | None:
            return "billing"  # a listed slug the session never mentions

    monkeypatch.setattr(summarize_module, "Summarizer", DriftingModel)
    cfg_file = tmp_path / "jezero.toml"
    cfg_file.write_text(
        f'''[storage]
path = "{config.db_path}"

[[connectors]]
name = "agent-claude"
type = "claude_code"
[connectors.options]
projects_dir = "{tmp_path / "claude"}"
'''
    )
    assert (
        cli_main(["-c", str(cfg_file), "retag", "--connector", "agent-claude", "--ask-model"]) == 0
    )
    (hit,) = store.search("Synthetic")
    assert hit.item.project == "Northwind"
    assert "1 answers not named" in capsys.readouterr().err
