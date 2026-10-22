"""SQLite + FTS5 storage.

One file. Writers (connectors) are serialised by SQLite; readers (the MCP server) run
concurrently thanks to WAL mode. Items live in a regular table; their searchable text
(title, summary, questions, atoms, project) is mirrored into an FTS5 table whose rowid is
the item id, kept in sync inside the same transaction.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .models import Item, NewItem, SearchHit, atoms_from_json

SCHEMA_VERSION = 1
MIN_SQLITE_FOR_TRIGRAM_DIACRITICS = (3, 45, 0)

# BM25 column weights: title, summary, questions, atoms, project.
BM25_WEIGHTS = (2.0, 1.0, 1.5, 1.5, 0.5)

UpsertOutcome = Literal["inserted", "updated", "unchanged"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL,
    questions     TEXT NOT NULL DEFAULT '[]',
    atoms         TEXT NOT NULL DEFAULT '[]',
    project       TEXT,
    account       TEXT,
    original      TEXT,
    original_ref  TEXT,
    trust         TEXT NOT NULL CHECK (trust IN ('own', 'external')),
    content_hash  TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS items_occurred ON items (occurred_at);
CREATE INDEX IF NOT EXISTS items_project ON items (project, occurred_at);
CREATE INDEX IF NOT EXISTS items_source ON items (source, occurred_at);
CREATE TABLE IF NOT EXISTS connector_state (
    name             TEXT PRIMARY KEY,
    cursor           TEXT,
    last_run_at      TEXT,
    last_success_at  TEXT,
    last_item_at     TEXT,
    last_error       TEXT
);
"""

_TERM_RE = re.compile(r'"([^"]+)"|(\S+)')


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectorState:
    name: str
    cursor: str | None = None
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_item_at: str | None = None
    last_error: str | None = None


def sqlite_version() -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in sqlite3.sqlite_version.split(".")[:3])
    return major, minor, patch


def build_match_query(query: str, *, min_term_length: int = 3) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    Every word or "quoted phrase" becomes a quoted FTS5 string, so user input can never
    inject FTS5 operators. Terms are OR-ed: BM25 ranks items matching more terms higher.
    Terms shorter than `min_term_length` are dropped (trigram tokenizers cannot match them).
    """
    parts: list[str] = []
    for phrase, word in _TERM_RE.findall(query):
        term = (phrase or word).strip()
        if len(term) < min_term_length:
            continue
        parts.append('"' + term.replace('"', '""') + '"')
    if not parts:
        return None
    return " OR ".join(dict.fromkeys(parts))


class Store:
    def __init__(self, path: Path, *, tokenizer: str = "trigram remove_diacritics 1") -> None:
        self.path = Path(path)
        self.tokenizer = tokenizer
        self._min_term = 3 if tokenizer.startswith("trigram") else 1

    # -- lifecycle -----------------------------------------------------------------

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly and not self.path.exists():
            raise StoreError(f"database not found at {self.path}; run `jezero init` first")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA foreign_keys = ON")
        if readonly:
            con.execute("PRAGMA query_only = ON")
        else:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        return con

    def init(self) -> None:
        self._check_sqlite_support()
        with self._write() as con:
            # executescript() would COMMIT behind our back; run statements one by one.
            for statement in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                con.execute(statement)
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                "title, summary, questions, atoms, project, "
                f"tokenize = '{self.tokenizer}')"
            )
            existing = dict(con.execute("SELECT key, value FROM meta").fetchall())
            if existing.get("tokenizer") not in (None, self.tokenizer):
                raise StoreError(
                    f"database was built with tokenizer {existing['tokenizer']!r} but config "
                    f"says {self.tokenizer!r}; rebuild the index or restore the old setting"
                )
            if int(existing.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
                raise StoreError("unsupported schema version; migrations are not implemented yet")
            con.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [("schema_version", str(SCHEMA_VERSION)), ("tokenizer", self.tokenizer)],
            )

    def _check_sqlite_support(self) -> None:
        tok = self.tokenizer
        needs_new_trigram = tok.startswith("trigram") and "remove_diacritics" in tok
        if needs_new_trigram and sqlite_version() < MIN_SQLITE_FOR_TRIGRAM_DIACRITICS:
            raise StoreError(
                f"SQLite {sqlite3.sqlite_version} is too old for '{tok}'; 3.45+ is required"
            )
        con = sqlite3.connect(":memory:")
        try:
            con.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize = '{self.tokenizer}')")
        except sqlite3.OperationalError as exc:
            raise StoreError(f"FTS5 tokenizer {self.tokenizer!r} unavailable: {exc}") from exc
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        con = self.connect(readonly=True)
        try:
            yield con
        finally:
            con.close()

    # -- writes --------------------------------------------------------------------

    def upsert(self, item: NewItem) -> UpsertOutcome:
        return self.upsert_many([item])[0]

    def upsert_many(self, items: Iterable[NewItem], *, force: bool = False) -> list[UpsertOutcome]:
        """Insert or update by (source, external_id). Same hash is a no-op unless `force`."""
        outcomes: list[UpsertOutcome] = []
        now = _now()
        with self._write() as con:
            for item in items:
                row = con.execute(
                    "SELECT id, content_hash FROM items WHERE source = ? AND external_id = ?",
                    (item.source, item.external_id),
                ).fetchone()
                values = _item_values(item)
                if row is None:
                    cur = con.execute(
                        "INSERT INTO items (source, external_id, occurred_at, ingested_at, title, "
                        "summary, questions, atoms, project, account, original, original_ref, "
                        "trust, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.source,
                            item.external_id,
                            values["occurred_at"],
                            now,
                            values["title"],
                            values["summary"],
                            values["questions"],
                            values["atoms"],
                            item.project,
                            item.account,
                            item.original,
                            item.original_ref,
                            item.trust,
                            item.content_hash,
                        ),
                    )
                    item_id = cur.lastrowid
                    outcomes.append("inserted")
                elif row["content_hash"] == item.content_hash and not force:
                    outcomes.append("unchanged")
                    continue
                else:
                    item_id = row["id"]
                    con.execute(
                        "UPDATE items SET occurred_at = ?, ingested_at = ?, title = ?, "
                        "summary = ?, questions = ?, atoms = ?, project = ?, account = ?, "
                        "original = ?, original_ref = ?, trust = ?, content_hash = ? "
                        "WHERE id = ?",
                        (
                            values["occurred_at"],
                            now,
                            values["title"],
                            values["summary"],
                            values["questions"],
                            values["atoms"],
                            item.project,
                            item.account,
                            item.original,
                            item.original_ref,
                            item.trust,
                            item.content_hash,
                            item_id,
                        ),
                    )
                    con.execute("DELETE FROM items_fts WHERE rowid = ?", (item_id,))
                    outcomes.append("updated")
                con.execute(
                    "INSERT INTO items_fts (rowid, title, summary, questions, atoms, project) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        item.title,
                        item.summary,
                        "\n".join(item.questions),
                        "\n".join(a.text for a in item.atoms),
                        item.project or "",
                    ),
                )
        return outcomes

    def known_hash(self, source: str, external_id: str) -> str | None:
        with self._read() as con:
            row = con.execute(
                "SELECT content_hash FROM items WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return row["content_hash"] if row else None

    def project_of(self, source: str, external_id: str) -> str | None:
        """Project tag of one item, or None if it is not stored."""
        with self._read() as con:
            row = con.execute(
                "SELECT project FROM items WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return (row["project"] or "") if row else None

    def set_project(self, source: str, external_id: str, project: str | None) -> bool:
        """Change one item's project tag (items and FTS). True if it changed."""
        with self._write() as con:
            row = con.execute(
                "SELECT id, project FROM items WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is None or row["project"] == project:
                return False
            con.execute("UPDATE items SET project = ? WHERE id = ?", (project, row["id"]))
            con.execute(
                "UPDATE items_fts SET project = ? WHERE rowid = ?", (project or "", row["id"])
            )
        return True

    def redact_stored(
        self, redact: Callable[[str], tuple[str, int]], *, dry_run: bool
    ) -> tuple[int, int]:
        """Apply a (newer) redactor to everything already stored, without re-summarising.

        `redact(text) -> (text, count)`. Rewrites title, summary, questions, atom texts and
        the stored original, and keeps the FTS mirror in sync. Returns (items, redactions).
        """
        changed_items = total = 0
        with self._write() as con:
            rows = con.execute(
                "SELECT id, title, summary, questions, atoms, project, original FROM items"
            ).fetchall()
            for row in rows:
                count = 0

                def clean(text: str | None) -> str | None:
                    nonlocal count
                    if not text:
                        return text
                    new, n = redact(text)
                    count += n
                    return new

                title, summary, original = (clean(row[k]) for k in ("title", "summary", "original"))
                questions = [clean(q) for q in json.loads(row["questions"] or "[]")]
                atoms = json.loads(row["atoms"] or "[]")
                for atom in atoms:
                    if isinstance(atom, dict) and isinstance(atom.get("text"), str):
                        atom["text"] = clean(atom["text"])
                if not count:
                    continue
                changed_items += 1
                total += count
                if dry_run:
                    continue
                con.execute(
                    "UPDATE items SET title = ?, summary = ?, questions = ?, atoms = ?, "
                    "original = ? WHERE id = ?",
                    (
                        title,
                        summary,
                        json.dumps(questions, ensure_ascii=False),
                        json.dumps(atoms, ensure_ascii=False),
                        original,
                        row["id"],
                    ),
                )
                con.execute("DELETE FROM items_fts WHERE rowid = ?", (row["id"],))
                con.execute(
                    "INSERT INTO items_fts (rowid, title, summary, questions, atoms, project) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        title,
                        summary,
                        "\n".join(q or "" for q in questions),
                        "\n".join(
                            str(a.get("text") or "") if isinstance(a, dict) else str(a)
                            for a in atoms
                        ),
                        row["project"] or "",
                    ),
                )
        return changed_items, total

    def prune_segments(self, source: str, session: str, keep: Sequence[str]) -> int:
        """Delete the items of `source` whose external_id is a segment of `session`
        (`<session>#...`) and not in `keep`. Returns how many were removed."""
        prefix, kept = session + "#", set(keep)
        with self._write() as con:
            rows = con.execute(
                "SELECT id, external_id FROM items WHERE source = ? "
                "AND substr(external_id, 1, ?) = ?",
                (source, len(prefix), prefix),
            ).fetchall()
            stale = [r["id"] for r in rows if r["external_id"] not in kept]
            con.executemany("DELETE FROM items_fts WHERE rowid = ?", [(i,) for i in stale])
            con.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in stale])
        return len(stale)

    def delete_source(self, source: str) -> int:
        with self._write() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM items WHERE source = ?", (source,))]
            con.executemany("DELETE FROM items_fts WHERE rowid = ?", [(i,) for i in ids])
            con.execute("DELETE FROM items WHERE source = ?", (source,))
        return len(ids)

    # -- connector state -----------------------------------------------------------

    def get_state(self, name: str) -> ConnectorState:
        with self._read() as con:
            row = con.execute("SELECT * FROM connector_state WHERE name = ?", (name,)).fetchone()
        return ConnectorState(**dict(row)) if row else ConnectorState(name=name)

    def set_state(self, state: ConnectorState) -> None:
        with self._write() as con:
            con.execute(
                "INSERT OR REPLACE INTO connector_state (name, cursor, last_run_at, "
                "last_success_at, last_item_at, last_error) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    state.name,
                    state.cursor,
                    state.last_run_at,
                    state.last_success_at,
                    state.last_item_at,
                    state.last_error,
                ),
            )

    def all_states(self) -> list[ConnectorState]:
        with self._read() as con:
            rows = con.execute("SELECT * FROM connector_state ORDER BY name").fetchall()
        return [ConnectorState(**dict(r)) for r in rows]

    def reset_cursor(self, name: str) -> bool:
        """Forget where a connector stopped, so its next run reads the source from the
        start. Items stay; the pipeline skips those whose content hash is unchanged.
        Returns whether there was a cursor to forget."""
        with self._write() as con:
            cur = con.execute(
                "UPDATE connector_state SET cursor = NULL WHERE name = ? AND cursor IS NOT NULL",
                (name,),
            )
            return cur.rowcount > 0

    # -- small key/value metadata -----------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._read() as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._write() as con:
            if value is None:
                con.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- reads ---------------------------------------------------------------------

    def sources(self) -> list[str]:
        with self._read() as con:
            return [r[0] for r in con.execute("SELECT DISTINCT source FROM items ORDER BY 1")]

    def resolve_sources(self, patterns: Sequence[str] | None) -> list[str] | None:
        """Expand glob patterns against stored sources. None means unrestricted."""
        if patterns is None or "*" in patterns:
            return None
        known = self.sources()
        return sorted({s for s in known for p in patterns if fnmatch.fnmatchcase(s, p)})

    def search(
        self,
        query: str,
        *,
        allowed_sources: Sequence[str] | None = None,
        source: str | None = None,
        project: str | None = None,
        since: datetime | None = None,
        limit: int = 10,
        candidate_pool: int = 200,
        recency_half_life_days: float = 90.0,
        recency_weight: float = 0.3,
        now: datetime | None = None,
    ) -> list[SearchHit]:
        match = build_match_query(query, min_term_length=self._min_term)
        if match is None:
            return []
        sources = self.resolve_sources(allowed_sources)
        if sources is not None and source is not None:
            sources = [s for s in sources if s == source]
        elif source is not None:
            sources = [source]
        if sources == []:
            return []

        where = ["items_fts MATCH ?"]
        params: list[object] = [match]
        if sources is not None:
            where.append(f"i.source IN ({','.join('?' * len(sources))})")
            params += sources
        if project is not None:
            where.append("i.project = ?")
            params.append(project)
        if since is not None:
            where.append("i.occurred_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        weights = ", ".join(str(w) for w in BM25_WEIGHTS)
        # Only constant weights, fixed column names and "?" placeholders are interpolated.
        sql = (
            f"SELECT i.*, bm25(items_fts, {weights}) AS bm25_score FROM items_fts "  # noqa: S608
            "JOIN items i ON i.id = items_fts.rowid "
            f"WHERE {' AND '.join(where)} ORDER BY bm25_score LIMIT ?"
        )
        params.append(candidate_pool)
        with self._read() as con:
            rows = con.execute(sql, params).fetchall()

        reference = now or datetime.now(UTC)
        hits: list[SearchHit] = []
        for row in rows:
            item = _row_to_item(row)
            relevance = -float(row["bm25_score"])
            age_days = max((reference - item.occurred_at).total_seconds() / 86400, 0.0)
            decay = 0.5 ** (age_days / recency_half_life_days)
            score = relevance * ((1.0 - recency_weight) + recency_weight * decay)
            hits.append(SearchHit(item=item, score=score, relevance=relevance))
        hits.sort(key=lambda h: h.score, reverse=True)
        return _one_per_copy(hits, key=lambda h: h.item)[:limit]

    def copies(self, item_ids: Sequence[int]) -> set[int]:
        """The given ids plus every item holding the same segment of the same original
        (same `original_ref` and `external_id`, in another source)."""
        found = set(item_ids)
        if not item_ids:
            return found
        marks = ",".join("?" * len(item_ids))
        with self._read() as con:
            rows = con.execute(
                "SELECT b.id FROM items a JOIN items b ON b.original_ref = a.original_ref "  # noqa: S608
                f"AND b.external_id = a.external_id WHERE a.id IN ({marks}) "
                "AND a.original_ref IS NOT NULL",
                list(item_ids),
            ).fetchall()
        return found | {r[0] for r in rows}

    def get(self, item_id: int, *, allowed_sources: Sequence[str] | None = None) -> Item | None:
        with self._read() as con:
            row = con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        item = _row_to_item(row)
        sources = self.resolve_sources(allowed_sources)
        if sources is not None and item.source not in sources:
            return None
        return item

    def timeline(
        self,
        *,
        allowed_sources: Sequence[str] | None = None,
        project: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
    ) -> list[Item]:
        sources = self.resolve_sources(allowed_sources)
        where: list[str] = []
        params: list[object] = []
        if sources is not None:
            if source is not None:
                sources = [s for s in sources if s == source]
            if not sources:
                return []
            where.append(f"source IN ({','.join('?' * len(sources))})")
            params += sources
        elif source is not None:
            where.append("source = ?")
            params.append(source)
        if project is not None:
            where.append("project = ?")
            params.append(project)
        if since is not None:
            where.append("occurred_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until is not None:
            where.append("occurred_at < ?")
            params.append(until.astimezone(UTC).isoformat())
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT * FROM items {clause} ORDER BY occurred_at DESC LIMIT ?"  # noqa: S608
        params.append(limit * 2 + 10)  # room for the copies dropped below
        with self._read() as con:
            items = [_row_to_item(r) for r in con.execute(sql, params).fetchall()]
        return _one_per_copy(items, key=lambda i: i)[:limit]

    def overview(
        self,
        *,
        allowed_sources: Sequence[str] | None = None,
        days: int = 14,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Counts for a dashboard: per source (total, latest, items per day over the last
        `days` UTC days, oldest first) and per project. Only `allowed_sources` are seen."""
        from datetime import timedelta

        sources = self.resolve_sources(allowed_sources)
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        day_list = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
        if sources is not None and not sources:
            # Nothing readable: still the same shape, so a dashboard can draw empty days.
            return {"items": 0, "days": day_list, "sources": {}, "projects": {}}
        where, params = "", []
        if sources is not None:
            where = f"WHERE source IN ({','.join('?' * len(sources))})"
            params = list(sources)
        with self._read() as con:
            per_source: dict[str, dict[str, object]] = {
                r["source"]: {"items": r["n"], "latest": r["latest"], "daily": [0] * days}
                for r in con.execute(
                    f"SELECT source, COUNT(*) AS n, MAX(occurred_at) AS latest FROM items "  # noqa: S608
                    f"{where} GROUP BY source ORDER BY source",
                    params,
                )
            }
            index = {d: i for i, d in enumerate(day_list)}
            joiner = "AND" if where else "WHERE"
            for r in con.execute(
                f"SELECT source, substr(occurred_at, 1, 10) AS day, COUNT(*) AS n FROM items "  # noqa: S608
                f"{where} {joiner} substr(occurred_at, 1, 10) >= ? GROUP BY source, day",
                [*params, day_list[0]],
            ):
                if r["day"] in index and r["source"] in per_source:
                    daily = per_source[r["source"]]["daily"]
                    assert isinstance(daily, list)
                    daily[index[r["day"]]] = r["n"]
            projects = {
                r["project"]: {"items": r["n"], "latest": r["latest"]}
                for r in con.execute(
                    f"SELECT project, COUNT(*) AS n, MAX(occurred_at) AS latest FROM items "  # noqa: S608
                    f"{where} {joiner} project IS NOT NULL GROUP BY project ORDER BY latest DESC",
                    params,
                )
            }
        total = sum(int(v["items"]) for v in per_source.values())  # type: ignore[call-overload]
        return {"items": total, "days": day_list, "sources": per_source, "projects": projects}

    def stats(self) -> dict[str, object]:
        with self._read() as con:
            per_source = {
                r["source"]: {"items": r["n"], "latest": r["latest"]}
                for r in con.execute(
                    "SELECT source, COUNT(*) AS n, MAX(occurred_at) AS latest "
                    "FROM items GROUP BY source ORDER BY source"
                )
            }
            total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return {"items": total, "sources": per_source}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _item_values(item: NewItem) -> dict[str, str]:
    return {
        "occurred_at": item.occurred_at.isoformat(),
        "title": item.title,
        "summary": item.summary,
        "questions": json.dumps(item.questions, ensure_ascii=False),
        "atoms": json.dumps([a.to_dict() for a in item.atoms], ensure_ascii=False),
    }


def _one_per_copy(entries: list, *, key) -> list:
    """Keep the first entry of each copy group, in order.

    Two sources can hold the same session (an agent's config folder copied to make
    another, or a session read both from a device inbox and from the host): their items
    share `original_ref` and `external_id`, the same segment of the same original. Showing
    both would spend two places of a result list on one answer.
    """
    seen: set[tuple[str, str]] = set()
    kept = []
    for entry in entries:
        item = key(entry)
        if item.original_ref:
            copy = (item.original_ref, item.external_id)
            if copy in seen:
                continue
            seen.add(copy)
        kept.append(entry)
    return kept


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        title=row["title"],
        summary=row["summary"],
        questions=json.loads(row["questions"]),
        atoms=atoms_from_json(row["atoms"]),
        project=row["project"],
        account=row["account"],
        original=row["original"],
        original_ref=row["original_ref"],
        trust=row["trust"],
        content_hash=row["content_hash"],
    )
