"""Regression tests for the core audit: connectors, pipeline, summarizer, redaction, eval,
project rules and CLI. Every secret below is fake and built at runtime, so no scanner flags
this file. No network: summarizers are fakes or httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jezero import cli
from jezero.config import Config, SummarizerConfig
from jezero.connectors.base import batch_full, truncate_middle
from jezero.connectors.codex import CodexConnector
from jezero.connectors.opencode import OpenCodeConnector
from jezero.evaluate import EvalFileError, load_cases
from jezero.models import NewItem, content_hash
from jezero.pipeline import run_connector
from jezero.projects import DEFAULT_RULES, ProjectRules
from jezero.redact import Redactor
from jezero.store import ConnectorState, Store
from jezero.summarize import Summarizer, SummarizerError, Summary, Usage

OLD = 1_700_000_000.0


class RecordingSummarizer:
    def __init__(self, fail_on: str | None = None, error: type[Exception] = TypeError) -> None:
        self.usage = Usage()
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.error = error

    async def summarize(self, text: str, *, title_hint=None, context=None) -> Summary:
        self.calls.append({"text": text, "title_hint": title_hint, "context": context})
        if self.fail_on and self.fail_on in text:
            raise self.error("unexpected failure inside the summarizer")
        return Summary("Synthetic title", "Synthetic summary.", ["q?"], [], None)


def rec(role: str, sid: str, content, **extra) -> dict:
    r = {
        "type": role,
        "sessionId": sid,
        "timestamp": "2030-03-17T00:00:00Z",
        "cwd": "/w/projects/demo",
        "message": {"role": role, "content": content},
    }
    r.update(extra)
    return r


def write_jsonl(path: Path, records: list, mtime: float = OLD, raw_lines: tuple = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "".join(raw_lines))
    os.utime(path, (mtime, mtime))
    return path


def config(tmp_path: Path, *, max_input_chars: int = 60_000, **options) -> Config:
    projects = tmp_path / "claude"
    projects.mkdir(exist_ok=True)
    return Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "summarizer": {"max_input_chars": max_input_chars},
            "connectors": [
                {
                    "name": "agent-claude",
                    "type": "claude_code",
                    "options": {"projects_dir": str(projects), **options},
                }
            ],
        }
    )


def store_for(cfg: Config) -> Store:
    s = Store(cfg.db_path)
    s.init()
    return s


def turns(sid: str, n: int, **extra) -> list[dict]:
    return [
        rec("user" if i % 2 == 0 else "assistant", sid, f"turn {i} " + "word " * 60, **extra)
        for i in range(n)
    ]


def run(cfg: Config, store: Store, summarizer=None, **kw):
    fake = summarizer or RecordingSummarizer()
    return asyncio.run(run_connector(cfg, store, cfg.connectors[0], summarizer=fake, **kw))


ROOT = "-w-projects-demo"


# -- 1. sub-agent transcripts get their own ids ------------------------------------------


def test_subagent_transcript_does_not_clobber_or_prune_parent(tmp_path: Path) -> None:
    cfg = config(tmp_path, max_input_chars=1_000, include_subagents=True)
    root = tmp_path / "claude" / ROOT
    # Real layout: <slug>/<sid>.jsonl and <slug>/<sid>/subagents/agent-*.jsonl, where every
    # record of the sub-agent file carries the PARENT's sessionId.
    write_jsonl(root / "sess-1.jsonl", turns("sess-1", 12), mtime=OLD)
    write_jsonl(
        root / "sess-1" / "subagents" / "agent-a.jsonl",
        turns("sess-1", 2, isSidechain=True),
        mtime=OLD + 1,
    )
    store = store_for(cfg)
    report = run(cfg, store)
    items = store.timeline(source="agent-claude", limit=100)
    assert report.pruned == 0
    assert len(items) == report.docs > 2
    ids = {i.external_id for i in items}
    sub = {i for i in ids if i.startswith("sess-1/agent-a#")}
    assert len(sub) == 1
    assert all(i.startswith("sess-1#") for i in ids - sub)
    refs = {i.original_ref for i in items if i.external_id in sub}
    assert refs == {"claude-code-session:sess-1/agent-a"}

    # A second run (nothing changed) keeps everything.
    store.set_state(ConnectorState(name="agent-claude"))
    again = run(cfg, store)
    assert again.pruned == 0 and again.unchanged == again.docs
    assert len(store.timeline(source="agent-claude", limit=100)) == len(items)


def test_parent_ids_do_not_change_without_subagents(tmp_path: Path) -> None:
    cfg = config(tmp_path, max_input_chars=1_000)
    root = tmp_path / "claude" / ROOT
    write_jsonl(root / "sess-1.jsonl", turns("sess-1", 6), mtime=OLD)
    write_jsonl(root / "sess-1" / "subagents" / "agent-a.jsonl", turns("sess-1", 2), mtime=OLD)
    store = store_for(cfg)
    run(cfg, store)
    items = store.timeline(source="agent-claude", limit=100)
    assert items and all(i.external_id.split("#")[0] == "sess-1" for i in items)
    assert {i.original_ref for i in items} == {"claude-code-session:sess-1"}


# -- 7. --limit never cuts a group of equal timestamps ------------------------------------


def test_limit_with_equal_mtimes_does_not_skip_sessions(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    root = tmp_path / "claude" / ROOT
    write_jsonl(root / "a.jsonl", turns("a", 4), mtime=OLD)
    write_jsonl(root / "b.jsonl", turns("b", 4), mtime=OLD)  # same second (cp -p, rsync)
    write_jsonl(root / "c.jsonl", turns("c", 4), mtime=OLD + 5)
    store = store_for(cfg)
    first = run(cfg, store, limit=1)
    assert first.docs == 2  # the whole group of equal timestamps
    run(cfg, store, limit=1)
    ids = sorted(i.external_id.split("#")[0] for i in store.timeline(source="agent-claude"))
    assert ids == ["a", "b", "c"]


def test_batch_full_extends_only_through_ties() -> None:
    assert not batch_full(0, 1.0, None, limit=1, sample=None)
    assert not batch_full(1, 1.0, 1.0, limit=1, sample=None)
    assert batch_full(1, 2.0, 1.0, limit=1, sample=None)
    assert not batch_full(5, 2.0, 1.0, limit=None, sample=None)
    assert batch_full(2, 1.0, 1.0, limit=None, sample=2)  # samples have no cursor


def codex_rollout(day: Path, sid: str, mtime: float) -> Path:
    lines = [
        {
            "type": "session_meta",
            "timestamp": "2033-05-18T00:00:00Z",
            "payload": {"id": sid, "cwd": "/w/projects/app"},
        },
        {
            "type": "response_item",
            "timestamp": "2033-05-18T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "x " * 400}],
            },
        },
    ]
    path = day / f"rollout-{sid}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    os.utime(path, (mtime, mtime))
    return path


def test_codex_limit_with_equal_mtimes(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2033" / "05" / "18"
    day.mkdir(parents=True)
    for sid in ("one", "two"):
        codex_rollout(day, sid, OLD)
    conn = CodexConnector(
        "cx", {"sessions_dir": str(tmp_path / "sessions")}, max_segment_chars=60_000
    )
    batch = conn.collect(ConnectorState(name="cx"), limit=1, now=OLD + 10_000)
    assert {d.external_id.split("#")[0] for d in batch.docs} == {"one", "two"}


# -- 2. malformed summarizer answers are SummarizerErrors; a batch keeps its summaries -----


def _openai_summarizer(handler, monkeypatch) -> Summarizer:
    monkeypatch.setenv("AUDIT_FAKE_KEY", "x")
    cfg = SummarizerConfig(
        provider="openai_compatible",
        base_url="https://llm.invalid/v1",
        model="m",
        api_key_env="AUDIT_FAKE_KEY",
    )
    return Summarizer(cfg, transport=httpx.MockTransport(handler))


def _anthropic_summarizer(handler, monkeypatch) -> Summarizer:
    monkeypatch.setenv("AUDIT_FAKE_KEY", "x")
    cfg = SummarizerConfig(
        provider="anthropic",
        base_url="https://llm.invalid",
        model="m",
        api_key_env="AUDIT_FAKE_KEY",
    )
    return Summarizer(cfg, transport=httpx.MockTransport(handler))


GOOD = {"title": "t", "summary": "s", "questions": [], "atoms": [], "project": None}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, text="<html>proxy</html>"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"choices": [{"message": {"content": "[1]"}}]}),
        httpx.Response(200, json={"usage": None, "choices": None}),
        httpx.Response(400, json=["not", "an", "object"]),
    ],
)
def test_openai_malformed_answers_are_summarizer_errors(response, monkeypatch) -> None:
    s = _openai_summarizer(lambda r: response, monkeypatch)
    with pytest.raises(SummarizerError):
        asyncio.run(s.summarize("doc"))


@pytest.mark.parametrize(
    "body",
    [
        {"content": None},
        {"content": [{"type": "tool_use", "input": None}]},
        {"content": ["text"], "usage": {"input_tokens": "lots"}},
    ],
)
def test_anthropic_malformed_answers_are_summarizer_errors(body, monkeypatch) -> None:
    s = _anthropic_summarizer(lambda r: httpx.Response(200, json=body), monkeypatch)
    with pytest.raises(SummarizerError):
        asyncio.run(s.summarize("doc"))


def _sessions_with_marker(tmp_path: Path, names: list[str]) -> None:
    root = tmp_path / "claude" / ROOT
    for n, sid in enumerate(names):
        records = turns(sid, 4)
        records[0]["message"]["content"] += f" marker-{sid}"
        write_jsonl(root / f"{sid}.jsonl", records, mtime=OLD + n)


def test_one_null_answer_does_not_discard_the_rest_of_the_run(tmp_path, monkeypatch) -> None:
    cfg = config(tmp_path)
    _sessions_with_marker(tmp_path, ["s1", "s2", "poison", "s3"])

    def handler(request):
        body = json.loads(request.content)
        if "marker-poison" in body["messages"][1]["content"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "choices": [{"message": {"content": json.dumps(GOOD)}}],
            },
        )

    store = store_for(cfg)
    report = run(cfg, store, _openai_summarizer(handler, monkeypatch))
    assert (report.stored, report.failed) == (3, 1)
    assert store.get_state("agent-claude").cursor is None  # the failed one is tried again


def test_unexpected_summarizer_exception_keeps_the_other_summaries(tmp_path) -> None:
    cfg = config(tmp_path)
    _sessions_with_marker(tmp_path, ["s1", "poison", "s2"])
    store = store_for(cfg)
    report = run(cfg, store, RecordingSummarizer(fail_on="marker-poison"))
    assert (report.stored, report.failed) == (2, 1)
    assert "TypeError" in report.errors[0]
    assert store.get_state("agent-claude").cursor is None


# -- 3. one malformed unit never blocks a connector ---------------------------------------


def test_lone_surrogate_in_one_session_does_not_block_others(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    root = tmp_path / "claude" / ROOT
    write_jsonl(root / "good.jsonl", turns("good", 4), mtime=OLD)
    # Node's JSON.stringify writes a split UTF-16 pair as an escaped lone surrogate.
    bad = json.dumps(rec("user", "bad", "emoji cut here \\ud83d " + "word " * 120))
    bad = bad.replace("\\\\ud83d", "\\ud83d")
    write_jsonl(root / "bad.jsonl", turns("bad", 2), mtime=OLD + 1, raw_lines=(bad + "\n",))
    store = store_for(cfg)
    report = run(cfg, store)
    assert (report.stored, report.failed) == (2, 0)
    stored = [i for i in store.timeline(source="agent-claude") if i.external_id.startswith("bad")]
    assert "\ufffd" in (stored[0].original or "")
    assert store.get_state("agent-claude").cursor == repr(OLD + 1)


def test_content_hash_accepts_a_lone_surrogate() -> None:
    assert content_hash("ok") == content_hash("ok")
    assert len(content_hash("half \ud83d emoji")) == 64


def test_corrupt_zst_rollout_does_not_block_the_codex_connector(tmp_path: Path) -> None:
    import zstandard

    day = tmp_path / "sessions" / "2033" / "05" / "18"
    day.mkdir(parents=True)
    good = codex_rollout(day, "ok", OLD)
    packed = zstandard.ZstdCompressor().compress(good.read_bytes() * 50)
    (day / "rollout-bad.jsonl.zst").write_bytes(packed[: len(packed) // 2])  # cut upload
    for p in day.iterdir():
        os.utime(p, (OLD, OLD))
    conn = CodexConnector(
        "cx", {"sessions_dir": str(tmp_path / "sessions")}, max_segment_chars=60_000
    )
    batch = conn.collect(ConnectorState(name="cx"), now=OLD + 10_000)
    assert any(d.external_id.startswith("ok#") for d in batch.docs)
    assert batch.next_cursor == repr(OLD)


def opencode_export(sid: str) -> dict:
    created = int(OLD * 1000)
    return {
        "session": {"id": sid, "directory": "/w/projects/demo", "title": "Fix the cache"},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": created}},
                "parts": [{"type": "text", "text": "Please fix the cache layer. " * 30}],
            }
        ],
    }


def test_opencode_export_that_is_not_an_object_is_skipped_for_good(tmp_path: Path) -> None:
    exports = tmp_path / "sessions"
    exports.mkdir()
    (exports / "ses_bad.json").write_text("[]")  # valid JSON, wrong shape
    (exports / "ses_good.json").write_text(json.dumps(opencode_export("ses_good")))
    os.utime(exports / "ses_bad.json", (OLD + 1, OLD + 1))
    os.utime(exports / "ses_good.json", (OLD, OLD))
    conn = OpenCodeConnector("oc", {"export_dir": str(exports)}, max_segment_chars=60_000)
    batch = conn.collect(ConnectorState(name="oc"), now=OLD + 10_000)
    assert [d.external_id.split("#")[0] for d in batch.docs] == ["ses_good"]
    assert batch.skipped and "ses_bad.json" in batch.skipped[0]
    assert batch.next_cursor == repr(OLD + 1)

    cfg = Config.model_validate(
        {
            "storage": {"path": str(tmp_path / "lib.db")},
            "connectors": [
                {"name": "oc", "type": "opencode", "options": {"export_dir": str(exports)}}
            ],
        }
    )
    store = store_for(cfg)
    report = run(cfg, store)
    state = store.get_state("oc")
    assert report.stored == 1 and report.warnings
    assert state.cursor == repr(OLD + 1)  # skipping does not hold the cursor
    assert "ses_bad.json" in (state.last_error or "")  # but the run reports it


# -- 4. redaction gaps ----------------------------------------------------------------------

LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP"
TG = "AAHdqTcvCH1vGWJxfSeofSAs" + "0K5PALDsaw"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
AWS_RANDOM = "Zq3vT8mK2pXw" + "9LbN4rYd7HsF1cGjU6eA" + "0tQ5oViE"
HEXISH = "abcdef0123456789" * 2

LEAKS = {
    "telegram_bot_token": ("bot " + "123456789:" + TG, TG),
    "telegram_in_url": ("https://api.telegram.org/bot" + "123456789:" + TG + "/getMe", TG),
    "slack_webhook": (
        "https://hooks.slack.com/services/" + "T00000000/B00000000/" + "X" * 24,
        "X" * 24,
    ),
    "discord_webhook": (
        "https://discord.com/api/webhooks/" + "123456789012345678/" + LETTERS[:46],
        LETTERS[:46],
    ),
    "authorization_token_scheme": (
        "Authorization: Token " + "9f8e7d6c5b4a3928" * 2,
        "9f8e7d6c5b4a3928",
    ),
    "authorization_raw": ('-H "Authorization: ' + 'abc123def456ghi789"', "abc123def456ghi789"),
    "cookie_header": ("Cookie: sessionid=" + HEXISH, HEXISH),
    "set_cookie_header": ("Set-Cookie: sid=" + HEXISH + "; HttpOnly", HEXISH),
    "redis_url_password_only": ("REDIS_URL=redis://:" + "p4ssw0rdXYZ@cache:6379/0", "p4ssw0rdXYZ"),
    "env_name_ending_in_KEY": ("OPENROUTER_KEY=" + "zz-v1-" + HEXISH, HEXISH),
    "django_secret_key": ("SECRET_KEY = '" + "django-insecure-" + HEXISH + "'", HEXISH),
    "groq_key": ("gsk_" + LETTERS, LETTERS),
    "xai_key": ("xai-" + LETTERS, LETTERS),
    "sendgrid_key": ("SG." + LETTERS[:22] + "." + LETTERS[:43], LETTERS[:43]),
    "pypi_token": ("pypi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdo", "AgEIcHlwaS5vcmcCJGFiY2RlZmdo"),
    "stripe_webhook_secret": ("whsec_" + LETTERS[:32], LETTERS[:32]),
    "pgp_private_key_block": (
        "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nVersion: x\n\nlQOYBF0AAAABCADxyz\n=abcd\n"
        "-----END PGP " + "PRIVATE KEY BLOCK-----",
        "lQOYBF0AAAABCADxyz",
    ),
    "aws_secret_near_key_id": ("AKIA" + "ABCDEFGHIJKLMNOP " + AWS_SECRET, AWS_SECRET),
    "aws_secret_alone": ("the value is " + AWS_RANDOM + " ok", AWS_RANDOM),
    "mysql_p_flag": ("mysql -u root -p" + "S3cretPassWord db", "S3cretPassWord"),
    "mysqldump_quoted_p_flag": ("mysqldump -u x '-p" + "S3cretPass' db", "S3cretPass"),
    "numeric_password": ("DB_PASSWORD=" + "93847561029384", "93847561029384"),
    "password_with_dots": ("password=" + "correct.horse.battery", "correct.horse.battery"),
    "spanish_contrasena": ("contraseña=" + "SuperSecreto123", "SuperSecreto123"),
    "spanish_clave": ("clave = " + "SuperSecreto123", "SuperSecreto123"),
    "spanish_clave_json": ('{"clave": "' + 'SuperSecreto123"}', "SuperSecreto123"),
}


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_redactor_hides(name: str) -> None:
    text, secret = LEAKS[name]
    out = Redactor().redact(text)
    assert secret not in out.text
    assert out.count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "commit 3f2a1b4c5d6e7f8091a2b3c4d5e6f708192a3b4c fixed the cursor",
        "git show 3F2A1B4C5D6E7F8091A2B3C4D5E6F708192A3B4C",
        "authorization: required for every endpoint",
        "Idea clave: revisar la configuración del cursor",
        "la clave del problema era el cursor",
        "sort_key = created_at",
        "primary_key=True",
        "password = settings.DB_PASSWORD",
        "password=self.password",
        "password: config.db_password",
        "mysql -u root -p mydb",
        "export API_KEY=$OPENROUTER_KEY",
        "redis://cache:6379/0",
        "The cookie: it was stale",
        "TestRedactorHidesSecretsInLongIdentifiers",
        "src/jezero/connectors/claude_code/helpers",
        "meeting at 12:30 with the team about the release",
    ],
)
def test_ordinary_text_is_not_redacted(text: str) -> None:
    assert Redactor().redact(text).count == 0


BEGIN = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
END = "-----END " + "OPENSSH PRIVATE KEY-----"
KEY_LINE = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"


def test_private_key_without_end_marker_is_redacted() -> None:
    cut_json = '{"content": "' + BEGIN + "\\n" + KEY_LINE + "\\n" + KEY_LINE[:30] + "..."
    assert KEY_LINE[:30] not in Redactor().redact(cut_json).text
    block = BEGIN + "\n" + "\n".join([KEY_LINE] * 20) + "\n" + END
    cut = truncate_middle(block, 400)
    assert "omitted" in cut
    assert KEY_LINE[:30] not in Redactor().redact(cut).text
    # The head cut away: the body is redacted back from the END line.
    tail = "notes\n\n" + "\n".join([KEY_LINE] * 3) + "\n" + END + "\nnext step"
    out = Redactor().redact(tail).text
    assert KEY_LINE[:30] not in out
    assert out.startswith("notes") and out.endswith("next step")


def test_private_key_cut_by_tool_arg_truncation_is_redacted(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    key = BEGIN + "\n" + "\n".join([KEY_LINE] * 4) + "\n" + END + "\n"
    tool = {
        "type": "tool_use",
        "id": "t1",
        "name": "Write",
        "input": {"file_path": "/home/u/.ssh/id_ed25519", "content": key},
    }
    records = turns("k", 4) + [rec("assistant", "k", [tool])]
    write_jsonl(tmp_path / "claude" / ROOT / "k.jsonl", records)
    fake = RecordingSummarizer()
    run(cfg, store_for(cfg), fake)
    assert KEY_LINE[:40] not in fake.calls[0]["text"]


# -- 6. titles and meta are redacted --------------------------------------------------------


def test_title_hint_and_meta_are_redacted_before_the_model_sees_them(tmp_path: Path) -> None:
    token = "ghp_" + "A1b2C3d4E5" * 4
    cfg = config(tmp_path)
    records = [{"type": "ai-title", "aiTitle": f"Rotate {token} in CI", "sessionId": "t"}]
    write_jsonl(tmp_path / "claude" / ROOT / "t.jsonl", records + turns("t", 4, entrypoint=token))
    fake = RecordingSummarizer()
    report = run(cfg, store_for(cfg), fake)
    assert token not in (fake.calls[0]["title_hint"] or "")
    assert token not in fake.calls[0]["context"]
    assert report.redactions >= 2


# -- 9. eval: types are checked --------------------------------------------------------------

CASE = '[[case]]\nid = "c"\nquestion = "quarterly invoice"\nexpect = [1]\n'


@pytest.mark.parametrize(
    "text",
    [
        CASE + 'sources = "agent-*"\n',
        '[defaults]\nsources = "agent-*"\n' + CASE,
        "[defaults]\nk = 0\n" + CASE,
        '[defaults]\nk = "5"\n' + CASE,
        "[defaults]\nfail_under = 1.5\n" + CASE,
        "[defaults]\nfail_under = true\n" + CASE,
        CASE + "sources = [1]\n",
        '[[case]]\nid = "c"\nquestion = "q"\nexpect = [true]\n',
    ],
)
def test_eval_file_with_wrong_types_is_rejected(tmp_path: Path, text: str) -> None:
    qfile = tmp_path / "q.toml"
    qfile.write_text(text)
    with pytest.raises(EvalFileError):
        load_cases(qfile)


def test_eval_file_with_right_types_loads(tmp_path: Path) -> None:
    qfile = tmp_path / "q.toml"
    qfile.write_text('[defaults]\nk = 5\nfail_under = 1\nsources = ["agent-*"]\n' + CASE)
    cases, defaults = load_cases(qfile)
    assert cases[0].expect == [1] and defaults["k"] == 5


# -- 10. Paperclip refs carry the company ----------------------------------------------------


def test_two_paperclip_companies_are_not_copies(tmp_path: Path, monkeypatch) -> None:
    from tests.test_paperclip import api, issue, make

    monkeypatch.setenv("PC_KEY", "k")
    transport, _ = api([issue("i1", "ACME-1")], {})
    conn = make(transport)
    with conn._client() as http:
        prod = conn._issue_docs(http, issue("i1", "ACME-1"), {}, "co-prod")
        lab = conn._issue_docs(http, issue("i1", "ACME-1"), {}, "co-lab")
    assert prod[0].original_ref == "paperclip-issue:co-prod/ACME-1"
    assert lab[0].original_ref == "paperclip-issue:co-lab/ACME-1"

    store = Store(tmp_path / "lib.db")
    store.init()
    for src, docs in (("paperclip-prod", prod), ("paperclip-lab", lab)):
        store.upsert(
            NewItem(
                source=src,
                external_id=docs[0].external_id,
                occurred_at=datetime.now(UTC),
                title=f"{src} checkout",
                summary=f"{src} checkout",
                original_ref=docs[0].original_ref,
            )
        )
    assert len(store.search("checkout")) == 2


# -- 11 and 13. CLI errors and the run lock --------------------------------------------------


def _cli_config(tmp_path: Path) -> Path:
    path = tmp_path / "jezero.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "lib.db"}"\n\n'
        f'[[connectors]]\nname = "agent-claude"\ntype = "claude_code"\n'
        f'[connectors.options]\nprojects_dir = "{tmp_path}"\n'
    )
    return path


def test_malformed_config_is_a_clean_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "jezero.toml"
    path.write_text("[storage\npath = 1\n")
    assert cli.main(["-c", str(path), "stats"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


def test_resummarize_with_a_bad_id_is_a_clean_error(tmp_path: Path, capsys) -> None:
    assert cli.main(["-c", str(_cli_config(tmp_path)), "resummarize", "--ids", "1,abc"]) == 2
    assert "not an item id: abc" in capsys.readouterr().err


def test_retag_and_resummarize_exit_3_while_a_run_holds_the_lock(tmp_path: Path, capsys) -> None:
    cfg = _cli_config(tmp_path)
    lock_path = (tmp_path / "lib.db").with_suffix(".run.lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(["-c", str(cfg), "retag", "--connector", "agent-claude"]) == 3
        assert cli.main(["-c", str(cfg), "resummarize", "--ids", "1"]) == 3
        err = capsys.readouterr().err
        assert err.count("another run is in progress") == 2
        assert cli.main(["-c", str(cfg), "run", "--due"]) == 0  # the timer keeps skipping


# -- 12. project rules ------------------------------------------------------------------------


def test_home_folder_named_like_a_root_is_not_a_root() -> None:
    rules = ProjectRules(git=False)
    assert rules.project_for("/home/dev/notes/todo") is None  # privacy: allow
    assert rules.project_for("/Users/code/notes") is None  # privacy: allow
    assert rules.project_for("/home/dev/projects/app2") == "app2"  # privacy: allow
    assert rules.project_for("/srv/dev/tool") == "tool"


def test_unicode_project_names() -> None:
    rules = ProjectRules(git=False)
    assert rules.project_for("/home/x/projects/diseño") == "diseño"
    decomposed = unicodedata.normalize("NFD", "diseño")
    assert rules.project_for(f"/home/x/projects/{decomposed}/src") == "diseño"
    assert rules.project_for("/home/x/projects/Ünïcode") == "ünïcode"
    assert (
        DEFAULT_RULES.with_aliases({"diseño": "design"}).project_for(
            f"/home/x/projects/{decomposed}"
        )
        == "design"
    )


def test_redacting_twice_counts_nothing_the_second_time() -> None:
    from jezero.redact import Redactor

    r = Redactor()
    text = (
        "curl -H 'Cookie: session=abc123def456' https://x.example\nAPI_KEY=sk_live_abcdefgh12345678"
    )
    first = r.redact(text)
    assert first.count >= 2
    second = r.redact(first.text)
    assert second.text == first.text and second.count == 0
