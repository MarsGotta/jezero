"""Second review, core: redaction false positives, transient read errors, terminal escape
codes in what is stored and shown, secrets in stored errors, Paperclip ref migration,
multi-frame Codex rollouts and clean config errors.

Every secret below is fake and built at runtime. No network: summarizers are fakes or
httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import zstandard
from rich.console import Console

from jezero import cli
from jezero.config import Config
from jezero.connectors.base import read_or_skip
from jezero.connectors.codex import CodexConnector
from jezero.models import Atom, NewItem, clean_display, clean_tree
from jezero.pipeline import RunReport, run_connector, scrub_error
from jezero.redact import Redactor
from jezero.store import ConnectorState, Store
from jezero.summarize import Summarizer, Summary, Usage, _parse
from jezero.ui.safe import CleanBackend, unwrap

ESC = "\x1b"
BEL = "\x07"
ST = ESC + "\\"
OSC52_BEL = ESC + "]52;c;" + "Y3VybCBldmlsLmV4YW1wbGUgfCBzaA==" + BEL
OSC52_ST = ESC + "]52;c;" + "cm0gLXJmIH4=" + ST
OSC8 = ESC + "]8;;https://evil.example/" + BEL + "click" + ESC + "]8;;" + BEL
CSI = ESC + "[2J" + ESC + "[31;1m"
C1_CSI = "\x9b" + "31m"


def _no_controls(text: str) -> bool:
    return not any(ord(c) < 0x20 and c not in "\n\t" or 0x7F <= ord(c) <= 0x9F for c in text)


# -- 1. redaction false positives -----------------------------------------------------------

ORDINARY = [
    "La contraseña: incorrecta, inténtalo otra vez",
    "Palabra-clave: arquitectura del sistema",
    'idempotency_key = "order-2024-10-23-0000001"',
    'partition_key: "tenant_12345_events_2026"',
    "Cookie: theme=dark",
    "cookie: http.cookies.BaseCookie[str] = http.cookies.SimpleCookie()",
    "build 20261023:" + "release_candidate_final_version_2 ok",
    "ssh mysql-host -p2222",
    "ssh mysql -p2222",
    "getUserProfileByIdV2AndOrganization4Test",
    "handleIncomingWebhookPayloadForTenant2026",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_kept(text: str) -> None:
    out = Redactor().redact(text)
    assert out.text == text
    assert out.count == 0


TG_SECRET = "AAHdqTcvCH1vGWJxfSeofSAs" + "0K5PALDsawQ"  # 35 characters
HEX32 = "abcdef0123456789" * 2

STILL_SECRET = {
    "contrasena_equals_word": ("contraseña=" + "incorrecta", "incorrecta"),
    "contrasena_colon_mixed": ("contraseña: " + "SuperSecreto123", "SuperSecreto123"),
    "clave_quoted_key": ('{"db_clave": "' + 'arquitectura"}', "arquitectura"),
    "telegram_alone_exact_shape": ("token 123456789:" + TG_SECRET, TG_SECRET),
    "telegram_after_bot": ("bot123456789:" + TG_SECRET[:30], TG_SECRET[:30]),
    "key_hex_value": ("cache_key = '" + HEX32 + "'", HEX32),
    "key_mixed_base64": ("signing_key: " + "Zq3vT8mK2pXw9LbN4rYd", "Zq3vT8mK2pXw9LbN4rYd"),
    "cookie_two_pairs": ("Cookie: theme=dark; " + "lang=en", "lang=en"),
    "cookie_session_name": ("Cookie: sessionid=" + "abc12345", "abc12345"),
    "cookie_long_opaque": ("Cookie: pref=" + HEX32, HEX32),
    "mysql_numeric_password": ("mysql -u root -p" + "123456 db", "123456"),
    "mysql_in_docker": ("docker exec -it db mysql -uroot -p" + "S3cretPass", "S3cretPass"),
    "remote_mysql_word_password": ("ssh db1 mysql -uroot -p" + "S3cretPass", "S3cretPass"),
}


@pytest.mark.parametrize("name", sorted(STILL_SECRET))
def test_real_secrets_are_still_redacted(name: str) -> None:
    text, secret = STILL_SECRET[name]
    out = Redactor().redact(text)
    assert secret not in out.text
    assert out.count >= 1


def test_redaction_stays_linear_on_long_hostile_lines() -> None:
    import time

    r = Redactor()
    hostile = [
        "Cookie: " + "a=" * 20_000,
        "x_key = " + "a-1" * 20_000,
        "contraseña: " + "a" * 50_000,
        "mysql " + "-p " * 20_000,
        "123456789:" * 5_000,
        "a_key=" * 8_000,
    ]
    # Linear means doubling the input roughly doubles the time; a quadratic rule gives 4x.
    # Ratios, not absolute times, so a slow CI runner does not fail the test.
    for text in hostile:
        half = text[: len(text) // 2]
        r.redact(half)
        start = time.perf_counter()
        r.redact(half)
        small = max(time.perf_counter() - start, 1e-3)
        start = time.perf_counter()
        r.redact(text)
        assert (time.perf_counter() - start) / small < 3.2, text[:20]


# -- 2. a damaged store or no memory keeps the cursor -----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.OperationalError("database is locked"),
        MemoryError(),
    ],
)
def test_transient_errors_fail_the_run_instead_of_skipping(error: BaseException) -> None:
    skipped: list[str] = []

    def read():
        raise error

    with pytest.raises(type(error)):
        read_or_skip("session x", skipped, read)
    assert skipped == []


def test_a_malformed_unit_is_still_skipped() -> None:
    skipped: list[str] = []

    def read():
        raise ValueError("not JSON")

    assert read_or_skip("file", skipped, read) is None
    assert skipped and "ValueError" in skipped[0]


# -- 3. terminal control characters ------------------------------------------------------------


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("copy " + OSC52_BEL + "this", "copy this"),
        ("copy " + OSC52_ST + "this", "copy this"),
        ("see " + OSC8 + " now", "see click now"),
        (CSI + "red" + ESC + "[0m", "red"),
        (C1_CSI + "red", "red"),
        ("a\x00b\x08c\x7fd\x85e\r", "abcde"),
        ("keep\nnew lines\tand tabs", "keep\nnew lines\tand tabs"),
        ("unterminated " + ESC + "]52;c;abc", "unterminated 52;c;abc"),
        ("charset " + ESC + "(B" + "ok" + ESC + "c", "charset ok"),
        ("plain text, ñ and emoji 🦀", "plain text, ñ and emoji 🦀"),
    ],
)
def test_clean_display(dirty: str, clean: str) -> None:
    assert clean_display(dirty) == clean


def test_clean_tree_cleans_nested_strings_only() -> None:
    value = {"title": "a" + CSI, "atoms": [{"kind": "fact", "text": OSC8}], "n": 3}
    assert clean_tree(value) == {"title": "a", "atoms": [{"kind": "fact", "text": "click"}], "n": 3}
    assert clean_tree(("x" + BEL, None)) == ("x", None)


def test_summarizer_output_is_cleaned() -> None:
    summary = _parse(
        {
            "title": "Fix " + OSC52_BEL + "the cursor",
            "summary": "Line one" + CSI + "\nline two " + OSC8,
            "questions": ["why " + OSC52_ST + "?"],
            "atoms": [{"kind": "command", "text": C1_CSI + "uv run pytest"}, "error: " + CSI],
            "project": "demo" + BEL,
        }
    )
    assert summary.title == "Fix the cursor"
    assert summary.summary == "Line one\nline two click"
    assert summary.questions == ["why ?"]
    assert summary.atoms == [Atom("command", "uv run pytest")]  # the empty error is dropped
    assert summary.project == "demo"


def _item(store: Store, title: str, **extra) -> int:
    store.upsert(
        NewItem(
            source="agent-claude",
            external_id=extra.pop("external_id", "s1#0"),
            occurred_at=datetime(2030, 3, 17, tzinfo=UTC),
            title=title,
            summary=extra.pop("summary", "Fixed the cursor " + CSI + "in the importer"),
            questions=extra.pop("questions", ["how " + OSC52_BEL + "cursor?"]),
            atoms=extra.pop("atoms", [Atom("fact", "cursor " + OSC8)]),
            original=extra.pop("original", "raw " + CSI + " transcript about the cursor"),
            **extra,
        )
    )
    return store.timeline(limit=1)[0].id


def _toml(tmp_path: Path) -> Path:
    path = tmp_path / "jezero.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        f'[[connectors]]\nname = "agent-claude"\ntype = "claude_code"\n'
        f'[connectors.options]\nprojects_dir = "{tmp_path}"\n'
    )
    return path


def test_cli_search_never_prints_escape_codes(tmp_path: Path, capsys) -> None:
    cfg = _toml(tmp_path)
    store = Store(tmp_path / "lib.db")
    store.init()
    _item(store, "Cursor " + OSC52_BEL + "fix" + CSI + "\nsecond line", project="demo" + BEL)
    assert cli.main(["-c", str(cfg), "search", "cursor"]) == 0
    out = capsys.readouterr().out
    assert _no_controls(out.replace("\n", ""))
    assert "Cursor fix second line" in out and "demo" in out
    assert out.count("\n") == 1


def test_redact_stored_cleans_controls_without_touching_hash_or_original(
    tmp_path: Path, capsys
) -> None:
    cfg = _toml(tmp_path)
    store = Store(tmp_path / "lib.db")
    store.init()
    item_id = _item(store, "Cursor " + OSC52_ST + "fix")
    _item(store, "Plain title", external_id="s2#0", summary="ok", questions=[], atoms=[])
    before = store.get(item_id)
    assert before is not None

    assert cli.main(["-c", str(cfg), "redact-stored", "--dry-run"]) == 0
    assert "would remove terminal control characters from 1 items" in capsys.readouterr().out
    assert store.get(item_id) == before

    assert cli.main(["-c", str(cfg), "redact-stored"]) == 0
    assert "removed terminal control characters from 1 items" in capsys.readouterr().out
    after = store.get(item_id)
    assert after is not None
    assert after.title == "Cursor fix"
    assert after.summary == "Fixed the cursor in the importer"
    assert after.questions == ["how cursor?"]
    assert after.atoms == [Atom("fact", "cursor click")]
    assert after.content_hash == before.content_hash
    assert after.original == before.original
    assert after.ingested_at == before.ingested_at
    assert [h.item.id for h in store.search("importer")] == [item_id]  # the index follows

    assert cli.main(["-c", str(cfg), "redact-stored"]) == 0
    assert "from 0 items" in capsys.readouterr().out


def test_import_jsonl_drops_controls(tmp_path: Path) -> None:
    cfg = _toml(tmp_path)
    data = tmp_path / "items.jsonl"
    record = {
        "external_id": "n1",
        "occurred_at": "2030-01-01T00:00:00+00:00",
        "title": "Imported " + OSC52_BEL + "note",
        "summary": "A summary " + CSI + "here",
        "questions": ["what " + C1_CSI + "?"],
        "atoms": [{"kind": "fact", "text": OSC8}],
    }
    data.write_text(json.dumps(record) + "\n")
    Store(tmp_path / "lib.db").init()
    assert cli.main(["-c", str(cfg), "import-jsonl", str(data), "--source", "notes"]) == 0
    item = Store(tmp_path / "lib.db").timeline(limit=1)[0]
    assert (item.title, item.summary) == ("Imported note", "A summary here")
    assert item.questions == ["what ?"]
    assert item.atoms == [Atom("fact", "click")]


class _DirtyBackend:
    label = "fake"
    now = datetime(2030, 3, 17, tzinfo=UTC)

    def _item(self) -> dict:
        return {
            "id": 7,
            "title": "Cursor " + OSC52_BEL + "fix",
            "summary": "Use " + OSC8 + " to " + CSI + "read",
            "atoms": [{"kind": "command", "text": C1_CSI + "uv run"}],
            "questions": ["why " + OSC52_ST + "?"],
            "project": "demo" + BEL,
            "source": "agent-claude",
            "occurred_at": "2030-03-17T00:00:00+00:00",
        }

    def search(self, query, project=None, limit=20):
        return [self._item()]

    def item(self, item_id, original=False):
        return self._item()

    def later(self, item_id, query=None):
        return [self._item() | {"id": 8}], "subject"

    def agents(self):
        return []


def test_clean_backend_cleans_server_answers() -> None:
    inner = _DirtyBackend()
    backend = CleanBackend(inner)
    assert unwrap(backend) is inner
    assert backend.label == "fake" and backend.now == inner.now
    assert _no_controls(json.dumps(backend.search("x"), ensure_ascii=False))
    newer, how = backend.later(7)
    assert how == "subject" and newer[0]["title"] == "Cursor fix"


def _render(renderable) -> str:
    console = Console(file=io.StringIO(), color_system=None, width=100)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def test_item_view_and_result_line_drop_controls() -> None:
    from jezero.ui.app import item_view, result_line
    from jezero.ui.look import NIGHT

    backend = _DirtyBackend()
    view = _render(item_view(backend._item(), NIGHT, [backend._item()]))
    assert _no_controls(view)
    assert "Cursor fix" in view and "uv run" in view
    assert _no_controls(_render(result_line(backend._item(), NIGHT)))


def test_people_commands_print_no_escape_codes(monkeypatch, capsys) -> None:
    from jezero.ui import commands

    monkeypatch.setattr(commands, "DemoBackend", _DirtyBackend)
    assert (
        commands.cmd_find(SimpleNamespace(query=["cursor"], project=None, limit=5, demo=True)) == 0
    )
    assert commands.cmd_show(SimpleNamespace(id=7, newer=True, demo=True)) == 0
    out = capsys.readouterr().out
    assert _no_controls(out)
    assert "Cursor fix" in out


def test_app_wraps_its_backend(monkeypatch) -> None:
    from jezero.ui.app import JezeroApp
    from jezero.ui.data import DemoBackend

    demo = DemoBackend()
    app = JezeroApp(backend=demo, demo=True)
    assert isinstance(app.backend, CleanBackend)
    assert app.now == demo.now


# -- 4. errors are redacted before they are stored -------------------------------------------

FAKE_KEY = "jzfake" + "Q7wPz2Lk9VbXr4"  # no known shape: only the literal match hides it


def _claude_config(tmp_path: Path, **summarizer) -> Config:
    projects = tmp_path / "claude"
    projects.mkdir(exist_ok=True)
    return Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": summarizer,
            "connectors": [
                {
                    "name": "agent-claude",
                    "type": "claude_code",
                    "options": {"projects_dir": str(projects)},
                }
            ],
        }
    )


def _session(tmp_path: Path) -> None:
    path = tmp_path / "claude" / "-w-projects-demo" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": role,
            "sessionId": "s1",
            "timestamp": "2030-03-17T00:00:00Z",
            "cwd": "/w/projects/demo",
            "message": {"role": role, "content": f"turn {i} " + "word " * 60},
        }
        for i, role in enumerate(["user", "assistant", "user", "assistant"])
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    os.utime(path, (1_700_000_000, 1_700_000_000))


def test_provider_error_echoing_the_key_is_not_stored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JZ_REVIEW_KEY", FAKE_KEY)
    cfg = _claude_config(
        tmp_path,
        provider="openai_compatible",
        base_url="https://llm.invalid/v1",
        model="m",
        api_key_env="JZ_REVIEW_KEY",
    )
    _session(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        message = f"Incorrect API key provided: {key}. Also tried password=hunter2hunter2"
        return httpx.Response(401, json={"error": {"message": message}})

    summarizer = Summarizer(cfg.summarizer, transport=httpx.MockTransport(handler))
    store = Store(cfg.db_path)
    store.init()
    report = asyncio.run(run_connector(cfg, store, cfg.connectors[0], summarizer=summarizer))
    assert report.failed == 1
    stored = store.get_state("agent-claude").last_error or ""
    for text in (stored, json.dumps(report.to_dict())):
        assert FAKE_KEY not in text
        assert "hunter2hunter2" not in text
        assert "[REDACTED:summarizer_api_key]" in text


def test_scrub_error_ignores_short_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JZ_SHORT_KEY", "x")
    cfg = _claude_config(tmp_path, api_key_env="JZ_SHORT_KEY")
    assert scrub_error("ValueError: x marks the spot", cfg) == "ValueError: x marks the spot"


def test_a_crashed_connector_stores_a_scrubbed_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JZ_REVIEW_KEY", FAKE_KEY)
    path = tmp_path / "jezero.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        '[summarizer]\napi_key_env = "JZ_REVIEW_KEY"\n\n'
        f'[[connectors]]\nname = "agent-claude"\ntype = "claude_code"\n'
        f'[connectors.options]\nprojects_dir = "{tmp_path}"\n'
    )

    async def boom(*args, **kwargs):
        raise RuntimeError(f"proxy said: bad key {FAKE_KEY}")

    monkeypatch.setattr("jezero.pipeline.run_connector", boom)
    assert cli.main(["-c", str(path), "run"]) == 1
    error = Store(tmp_path / "lib.db").get_state("agent-claude").last_error or ""
    assert FAKE_KEY not in error and "[REDACTED:summarizer_api_key]" in error


# -- 5. Paperclip refs move to the new form without new summaries ------------------------------


class _CountingSummarizer:
    def __init__(self) -> None:
        self.usage = Usage()
        self.calls = 0

    async def summarize(self, text: str, *, title_hint=None, context=None) -> Summary:
        self.calls += 1
        return Summary("Ship the checkout", "Checkout moved.", ["q?"], [], None)


def test_connector_reset_and_run_refresh_old_paperclip_refs(tmp_path: Path, monkeypatch) -> None:
    from tests.test_paperclip import api, issue, make

    monkeypatch.setenv("PC_KEY", "k")
    past = "2026-01-01T00:00:00Z"
    stamps = {"createdAt": past, "updatedAt": past, "lastActivityAt": past}
    transport, _ = api([issue("i1", "ACME-1", **stamps), issue("i2", "ACME-2", **stamps)], {})
    path = tmp_path / "jezero.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        '[[connectors]]\nname = "pc"\ntype = "paperclip"\n'
        '[connectors.options]\napi_url = "http://pc.invalid/api"\napi_key_env = "PC_KEY"\n'
    )
    cfg = cli.load_config(str(path))
    monkeypatch.setattr("jezero.pipeline.build_connector", lambda config, conn: make(transport))
    store = Store(cfg.db_path)
    store.init()

    first = _CountingSummarizer()
    asyncio.run(run_connector(cfg, store, cfg.connectors[0], summarizer=first))
    assert first.calls == 2
    con = store.connect()
    con.execute("UPDATE items SET original_ref = 'paperclip-issue:' || substr(external_id, 1, 6)")
    con.close()
    before = {i.external_id: i for i in store.timeline(limit=10)}
    assert {i.original_ref for i in before.values()} == {
        "paperclip-issue:ACME-1",
        "paperclip-issue:ACME-2",
    }

    assert cli.main(["-c", str(path), "connector", "reset", "pc", "--yes"]) == 0
    again = _CountingSummarizer()
    report = asyncio.run(run_connector(cfg, store, cfg.connectors[0], summarizer=again))
    assert again.calls == 0
    assert (report.unchanged, report.stored, report.refs_updated) == (2, 0, 2)
    assert report.to_dict()["refs_updated"] == 2
    after = {i.external_id: i for i in store.timeline(limit=10)}
    assert {i.original_ref for i in after.values()} == {
        "paperclip-issue:co-1/ACME-1",
        "paperclip-issue:co-1/ACME-2",
    }
    for key, item in after.items():  # nothing else moved
        assert (item.title, item.content_hash, item.ingested_at) == (
            before[key].title,
            before[key].content_hash,
            before[key].ingested_at,
        )

    third = asyncio.run(run_connector(cfg, store, cfg.connectors[0], summarizer=again))
    assert third.refs_updated == 0 and "refs_updated" not in third.to_dict()


def test_report_without_ref_changes_keeps_its_shape() -> None:
    assert "refs_updated" not in RunReport(connector="x").to_dict()


# -- 6. multi-frame zstd rollouts ---------------------------------------------------------------


def test_codex_reads_every_zstd_frame(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2033" / "05" / "18"
    day.mkdir(parents=True)

    def line(kind: str, payload: dict) -> bytes:
        return (
            json.dumps({"type": kind, "timestamp": "2033-05-18T00:00:00Z", "payload": payload})
            + "\n"
        ).encode()

    def message(text: str) -> dict:
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }

    first = line("session_meta", {"id": "two-frames", "cwd": "/w/projects/app"})
    first += line("response_item", message("first frame " + "x " * 300))
    second = line("response_item", message("second-frame-marker " + "y " * 300))
    compressor = zstandard.ZstdCompressor()
    path = day / "rollout-two-frames.jsonl.zst"
    path.write_bytes(compressor.compress(first) + compressor.compress(second))
    os.utime(path, (1_700_000_000, 1_700_000_000))

    conn = CodexConnector(
        "cx", {"sessions_dir": str(tmp_path / "sessions")}, max_segment_chars=60_000
    )
    batch = conn.collect(ConnectorState(name="cx"), now=1_700_100_000)
    text = "\n".join(d.text for d in batch.docs)
    assert "first frame" in text and "second-frame-marker" in text


# -- 7 and 8. CLI help and config errors ------------------------------------------------------


def test_run_limit_help_says_it_may_exceed_n(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "may take more than N" in help_text and "share a timestamp" in help_text


def test_invalid_config_is_one_clean_line_per_problem(tmp_path: Path, capsys) -> None:
    path = tmp_path / "jezero.toml"
    secret = "do-not-print-" + "this-value-123"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        f'[server]\nport = "{secret}"\n\n'
        '[[connectors]]\nname = "x"\ntype = "claude_code"\ninterval_minutes = "often"\n'
    )
    assert cli.main(["-c", str(path), "stats"]) == 2
    err = capsys.readouterr().err
    lines = err.strip().splitlines()
    assert lines and all(line.startswith("invalid config: ") for line in lines)
    assert any("server.port:" in line for line in lines)
    assert any("connectors[0].interval_minutes:" in line for line in lines)
    assert secret not in err and "often" not in err
    assert "pydantic.dev" not in err and "Traceback" not in err


def test_format_validation_error_without_location() -> None:
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        a: int

    with pytest.raises(ValidationError) as caught:
        M.model_validate({"a": "nope-secret"})
    assert cli.format_validation_error(caught.value) == [
        "a: Input should be a valid integer, unable to parse string as an integer"
    ]
