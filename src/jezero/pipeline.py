"""Run a connector end to end: collect, redact, skip unchanged, summarise, store."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config, ConnectorConfig
from .connectors import CONNECTOR_TYPES
from .connectors.base import BuildContext, Connector, RawDoc
from .models import NewItem, content_hash
from .projects import ProjectRules
from .redact import Redactor
from .store import ConnectorState, Store
from .summarize import Summarizer, SummarizerError

CHUNK = 16
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
"""Half of a UTF-16 pair, as JSON-escaped by a tool that cut an emoji in two (`\\ud83d`).
Python reads it into a string that cannot be encoded as UTF-8, which the hash, the HTTP
request and SQLite all need."""


@dataclass(slots=True)
class RunReport:
    connector: str
    docs: int = 0
    unchanged: int = 0
    stored: int = 0
    failed: int = 0
    deferred_sessions: int = 0
    redactions: int = 0
    input_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    pruned: int = 0
    """Stored segments removed because their session no longer produces them."""
    refs_updated: int = 0
    """Unchanged items whose `original_ref` the source now gives differently (a new ref
    format): updated in place, without summarising them again."""
    dry_run: bool = False
    sample: bool = False
    note: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["errors"] = self.errors[:5]
        if not self.refs_updated:
            del data["refs_updated"]  # only shown when it happened, as after a ref change
        return data


def connector_rules(config: Config, conn: ConnectorConfig) -> ProjectRules:
    """The project rules with the connector's own `project_aliases` on top."""
    return project_rules(config).with_aliases(dict(conn.options.get("project_aliases") or {}))


def project_rules(config: Config) -> ProjectRules:
    p = config.projects
    return ProjectRules(paths=p.paths, roots=p.roots, aliases=p.aliases, git=p.git)


def build_connector(config: Config, conn: ConnectorConfig) -> Connector:
    kind = CONNECTOR_TYPES[conn.type]
    if kind.status != "available":
        raise NotImplementedError(f"connector type {conn.type!r} is not implemented yet")
    if kind.build is None:
        raise NotImplementedError(f"connector type {conn.type!r} cannot run on a schedule")
    context = BuildContext(
        max_segment_chars=config.summarizer.max_input_chars,
        project_rules=project_rules(config),
    )
    return kind.build(conn, context)


async def run_connector(
    config: Config,
    store: Store,
    conn: ConnectorConfig,
    *,
    limit: int | None = None,
    sample: int | None = None,
    dry_run: bool = False,
    summarizer: Summarizer | None = None,
) -> RunReport:
    """Collect, redact, summarise what changed and store it.

    A `sample` run is a pilot: it stores what it summarises but leaves the connector's
    state (cursor, last run, last success) alone, so the schedule and `jezero health` see
    the connector as never run until a real run. Without anything to summarise no
    summarizer is needed, so a pilot of an idle source works before the model is set.
    """
    report = RunReport(connector=conn.name, dry_run=dry_run, sample=sample is not None)
    connector = build_connector(config, conn)
    state = store.get_state(conn.name)
    batch = connector.collect(state, limit=limit, sample=sample)
    report.docs = len(batch.docs)
    report.deferred_sessions = batch.deferred
    report.warnings = list(batch.warnings) + list(batch.skipped)
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)

    failed_ids: set[str] = set()
    pending: list[tuple[RawDoc, str, str]] = []
    refs: list[tuple[str, str]] = []
    for doc in batch.docs:
        try:
            text = _prepare(doc, redactor, report)
            digest = content_hash(text)
            known = store.known_hash(conn.name, doc.external_id) == digest
        except Exception as exc:  # noqa: BLE001 - one bad unit must not stop the batch
            _record_failure(report, failed_ids, doc.external_id, exc)
            continue
        if known:
            report.unchanged += 1
            if doc.original_ref:
                refs.append((doc.external_id, doc.original_ref))
            continue
        report.input_chars += len(text)
        pending.append((doc, text, digest))

    if not pending:
        report.note = "nothing to summarise" + (
            f" ({report.deferred_sessions} still active, deferred)"
            if report.deferred_sessions
            else ""
        )
    if dry_run:
        _scrub_report(report, config, redactor)
        return report

    try:
        report.refs_updated = _refresh_refs(store, conn.name, refs)
    except Exception as exc:  # noqa: BLE001 - a ref left as it was is no reason to stop
        report.warnings.append(f"original_ref not updated: {type(exc).__name__}: {exc}")
    if pending:
        summarizer = summarizer or Summarizer(
            config.summarizer, known_projects=config.summarizer.known_projects
        )
        await _summarise_and_store(
            store, conn, pending, summarizer, report, failed_ids, connector_rules(config, conn)
        )
        report.input_tokens = summarizer.usage.input_tokens
        report.output_tokens = summarizer.usage.output_tokens
    report.pruned = _prune_stale_segments(store, conn.name, batch.docs, failed_ids)
    _scrub_report(report, config, redactor)
    if sample is not None:
        return report  # a pilot: cursor, last run and last success stay untouched

    now = datetime.now(UTC).isoformat()
    ok = report.failed == 0
    # Skipped units do not hold the cursor (reading them again cannot help); warnings do.
    advance = ok and not batch.warnings and batch.next_cursor
    problems = report.errors[:3] + report.warnings[:2]
    store.set_state(
        ConnectorState(
            name=conn.name,
            cursor=batch.next_cursor if advance else state.cursor,
            last_run_at=now,
            last_success_at=now if ok else state.last_success_at,
            last_item_at=now if report.stored else state.last_item_at,
            last_error="; ".join(problems)[:1000] if problems else None,
        )
    )
    return report


def _refresh_refs(store: Store, source: str, refs: list[tuple[str, str]]) -> int:
    """Set `original_ref` of stored items whose text is unchanged but whose source now
    gives another ref (Paperclip went from `paperclip-issue:ACME-1` to
    `paperclip-issue:<company>/ACME-1`). No summary is made again. Returns how many changed.

    The store has no method for this one column, so the UPDATE is done here on the store's
    own connection; the search index does not hold `original_ref` and needs no change.
    """
    if not refs:
        return 0
    con = store.connect()
    changed = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        for external_id, ref in refs:
            cur = con.execute(
                "UPDATE items SET original_ref = ? "
                "WHERE source = ? AND external_id = ? AND original_ref IS NOT ?",
                (ref, source, external_id, ref),
            )
            changed += max(cur.rowcount, 0)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return changed


def scrub_error(text: str, config: Config, redactor: Redactor | None = None) -> str:
    """`text` (an error to print or store as `last_error`) without secrets: the redaction
    rules, then the summarizer's API key itself, which a provider or a proxy may echo back
    in its error message whatever its shape."""
    redactor = redactor or Redactor(
        config.redaction.extra_patterns, enabled=config.redaction.enabled
    )
    text = redactor.redact(text).text
    key = os.environ.get(config.summarizer.api_key_env) or ""
    key = key.strip()
    if len(key) >= 8 and key in text:  # shorter values are too likely ordinary text
        text = text.replace(key, "[REDACTED:summarizer_api_key]")
    return text


def _scrub_report(report: RunReport, config: Config, redactor: Redactor) -> None:
    report.errors = [scrub_error(e, config, redactor) for e in report.errors]
    report.warnings = [scrub_error(w, config, redactor) for w in report.warnings]


def _clean(text: str) -> str:
    return _LONE_SURROGATE.sub("\ufffd", text) if _LONE_SURROGATE.search(text) else text


def _prepare(doc: RawDoc, redactor: Redactor, report: RunReport) -> str:
    """Make `doc` safe to send and store: lone surrogates replaced, secrets redacted in the
    text and in what goes to the model with it (the title hint and the meta values).
    Returns the redacted text."""
    cleaned = redactor.redact(_clean(doc.text))
    report.redactions += cleaned.count
    if doc.title_hint:
        title = redactor.redact(_clean(doc.title_hint))
        report.redactions += title.count
        doc.title_hint = title.text
    doc.meta = {k: _redact_value(v, redactor, report) for k, v in doc.meta.items()}
    return cleaned.text


def _redact_value(value: Any, redactor: Redactor, report: RunReport) -> Any:
    if isinstance(value, str):
        result = redactor.redact(_clean(value))
        report.redactions += result.count
        return result.text
    if isinstance(value, list | tuple):
        return [_redact_value(v, redactor, report) for v in value]
    return value


def _record_failure(report: RunReport, failed_ids: set[str], key: str, exc: BaseException) -> None:
    report.failed += 1
    failed_ids.add(key)
    detail = str(exc) if isinstance(exc, SummarizerError) else f"{type(exc).__name__}: {exc}"
    report.errors.append(f"{key}: {detail}")


async def _summarise_and_store(
    store: Store,
    conn: ConnectorConfig,
    pending: list[tuple[RawDoc, str, str]],
    summarizer: Summarizer,
    report: RunReport,
    failed_ids: set[str],
    rules: ProjectRules | None = None,
) -> None:
    for start in range(0, len(pending), CHUNK):
        chunk = pending[start : start + CHUNK]
        results = await asyncio.gather(
            *(
                summarizer.summarize(
                    text,
                    title_hint=doc.title_hint,
                    context=f"source={conn.name} "
                    + " ".join(f"{k}={v}" for k, v in doc.meta.items()),
                )
                for doc, text, _ in chunk
            ),
            return_exceptions=True,
        )
        items: list[NewItem] = []
        for (doc, text, digest), result in zip(chunk, results, strict=True):
            if isinstance(result, asyncio.CancelledError | KeyboardInterrupt):
                raise result
            if isinstance(result, BaseException):
                # Paid summaries of the rest of the chunk are still stored; this one keeps
                # the cursor where it was, so the next run tries it again.
                _record_failure(report, failed_ids, doc.external_id, result)
                continue
            try:
                item = NewItem(
                    source=conn.name,
                    external_id=doc.external_id,
                    occurred_at=doc.occurred_at,
                    title=result.title,
                    summary=result.summary,
                    questions=result.questions,
                    atoms=result.atoms,
                    # A hint from the source itself (working directory, project field)
                    # beats the model's guess, which follows whatever the text mentions;
                    # the guess still goes through the aliases.
                    project=doc.project_hint
                    or (rules.rename(result.project) if rules else result.project),
                    original=text,
                    original_ref=doc.original_ref,
                    trust=conn.effective_trust,
                    content_hash=digest,
                )
            except Exception as exc:  # noqa: BLE001 - one bad unit must not stop the batch
                _record_failure(report, failed_ids, doc.external_id, exc)
                continue
            items.append(item)
        report.stored += sum(1 for o in store.upsert_many(items) if o != "unchanged")


def _prune_stale_segments(
    store: Store, source: str, docs: list[RawDoc], failed_ids: set[str]
) -> int:
    """Remove stored segments of a session the connector re-emitted whole, when their id
    is not among the current ones (the split changed with `summarizer.max_input_chars`, or
    the session shrank). Sessions with a segment that failed keep their old items."""
    sessions: dict[str, tuple[str, ...]] = {}
    for doc in docs:
        if doc.session is not None and doc.session_segments:
            sessions[doc.session] = doc.session_segments
    removed = 0
    for session, keep in sessions.items():
        if failed_ids.intersection(keep):
            continue
        removed += store.prune_segments(source, session, keep)
    return removed


async def resummarize_items(
    config: Config,
    store: Store,
    item_ids: list[int],
    *,
    summarizer: Summarizer | None = None,
) -> RunReport:
    """Summarise stored items again from their kept original text (for example after
    changing `summarizer.output_language`). Source, ids, dates and project stay."""
    report = RunReport(connector="resummarize")
    summarizer = summarizer or Summarizer(
        config.summarizer, known_projects=config.summarizer.known_projects
    )
    items = [i for i in (store.get(n) for n in item_ids) if i is not None and i.original]
    report.docs = len(items)
    report.input_chars = sum(len(i.original or "") for i in items)
    for start in range(0, len(items), CHUNK):
        chunk = items[start : start + CHUNK]
        results = await asyncio.gather(
            *(
                summarizer.summarize(
                    item.original or "",
                    context=f"source={item.source} project={item.project or '-'}",
                )
                for item in chunk
            ),
            return_exceptions=True,
        )
        fresh: list[NewItem] = []
        for item, result in zip(chunk, results, strict=True):
            if isinstance(result, asyncio.CancelledError | KeyboardInterrupt):
                raise result
            if isinstance(result, BaseException):
                _record_failure(report, set(), str(item.id), result)
                continue
            fresh.append(
                NewItem(
                    source=item.source,
                    external_id=item.external_id,
                    occurred_at=item.occurred_at,
                    title=result.title,
                    summary=result.summary,
                    questions=result.questions,
                    atoms=result.atoms,
                    project=item.project or result.project,
                    account=item.account,
                    original=item.original,
                    original_ref=item.original_ref,
                    trust=item.trust,
                    content_hash=item.content_hash,
                )
            )
        report.stored += sum(1 for o in store.upsert_many(fresh, force=True) if o == "updated")
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)
    _scrub_report(report, config, redactor)
    report.input_tokens = summarizer.usage.input_tokens
    report.output_tokens = summarizer.usage.output_tokens
    return report
