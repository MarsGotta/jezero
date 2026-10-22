"""Run a connector end to end: collect, redact, skip unchanged, summarise, store."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .config import Config, ConnectorConfig
from .connectors import CONNECTOR_TYPES
from .connectors.base import BuildContext, Connector, RawDoc
from .models import NewItem, content_hash
from .projects import ProjectRules
from .redact import Redactor
from .store import ConnectorState, Store
from .summarize import Summarizer, SummarizerError

CHUNK = 16


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
    dry_run: bool = False
    sample: bool = False
    note: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["errors"] = self.errors[:5]
        return data


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
    report.warnings = list(batch.warnings)
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)

    pending: list[tuple[RawDoc, str, str]] = []
    for doc in batch.docs:
        cleaned = redactor.redact(doc.text)
        report.redactions += cleaned.count
        digest = content_hash(cleaned.text)
        if store.known_hash(conn.name, doc.external_id) == digest:
            report.unchanged += 1
            continue
        report.input_chars += len(cleaned.text)
        pending.append((doc, cleaned.text, digest))

    if not pending:
        report.note = "nothing to summarise" + (
            f" ({report.deferred_sessions} still active, deferred)"
            if report.deferred_sessions
            else ""
        )
    if dry_run:
        return report

    failed_ids: set[str] = set()
    if pending:
        summarizer = summarizer or Summarizer(
            config.summarizer, known_projects=config.summarizer.known_projects
        )
        await _summarise_and_store(store, conn, pending, summarizer, report, failed_ids)
        report.input_tokens = summarizer.usage.input_tokens
        report.output_tokens = summarizer.usage.output_tokens
    report.pruned = _prune_stale_segments(store, conn.name, batch.docs, failed_ids)
    if sample is not None:
        return report  # a pilot: cursor, last run and last success stay untouched

    now = datetime.now(UTC).isoformat()
    ok = report.failed == 0
    advance = ok and not report.warnings and batch.next_cursor
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


async def _summarise_and_store(
    store: Store,
    conn: ConnectorConfig,
    pending: list[tuple[RawDoc, str, str]],
    summarizer: Summarizer,
    report: RunReport,
    failed_ids: set[str],
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
            if isinstance(result, BaseException):
                report.failed += 1
                report.errors.append(f"{doc.external_id}: {result}")
                failed_ids.add(doc.external_id)
                if not isinstance(result, SummarizerError):
                    raise result
                continue
            items.append(
                NewItem(
                    source=conn.name,
                    external_id=doc.external_id,
                    occurred_at=doc.occurred_at,
                    title=result.title,
                    summary=result.summary,
                    questions=result.questions,
                    atoms=result.atoms,
                    # A hint from the source itself (working directory, project field)
                    # beats the model's guess, which follows whatever the text mentions.
                    project=doc.project_hint or result.project,
                    original=text,
                    original_ref=doc.original_ref,
                    trust=conn.effective_trust,
                    content_hash=digest,
                )
            )
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
            if isinstance(result, BaseException):
                report.failed += 1
                report.errors.append(f"{item.id}: {result}")
                if not isinstance(result, SummarizerError):
                    raise result
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
    report.input_tokens = summarizer.usage.input_tokens
    report.output_tokens = summarizer.usage.output_tokens
    return report
