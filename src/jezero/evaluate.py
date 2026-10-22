"""Measure search quality with a fixed set of questions and the items that answer them.

A question file (TOML) lists cases. Each case is a question as someone would ask it and
the ids of the items that answer it; a case passes when one of those ids is in the top
`k` results (a copy of one of them, the same session read from another source, counts
too). `later` cases check the `later` tool instead: starting from an item that
holds an old decision, one of the expected newer items must come back.

The score is the same search the MCP server runs (BM25 plus the recency boost from
`[search]`), read with the sources of a client, so a run measures what an agent sees
before it rephrases anything. Save a run with `--save` and pass it to `--compare` after a
change to see which cases got better or worse.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

KINDS = ("search", "later")


class EvalFileError(ValueError):
    """The question file cannot be used; the message says which case and why."""


@dataclass(slots=True)
class Case:
    id: str
    question: str
    expect: list[int]
    kind: str = "search"
    query: str | None = None
    """Words to search with instead of the question (what an agent would type)."""
    item: int | None = None
    """For `later` cases: the item holding the old state."""
    sources: list[str] | None = None
    project: str | None = None
    note: str = ""


@dataclass(slots=True)
class CaseResult:
    id: str
    kind: str
    passed: bool
    rank: int | None
    """1-based position of the first expected id, None when it is not in the results."""
    got: list[int]
    missing: list[int] = field(default_factory=list)
    """Expected ids that are no longer in the library (the key needs updating)."""


@dataclass(slots=True)
class EvalReport:
    k: int
    cases: int
    passed: int
    hit_at_1: float
    hit_at_k: float
    mrr: float
    stale: int
    results: list[CaseResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_cases(path: Path) -> tuple[list[Case], dict[str, Any]]:
    """Read a question file. Returns the cases and the `[defaults]` table."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EvalFileError(f"{path}: {exc}") from exc
    defaults = dict(data.get("defaults") or {})
    unknown = set(defaults) - {"k", "sources", "fail_under"}
    if unknown:
        raise EvalFileError(f"[defaults]: unknown keys {sorted(unknown)}")
    cases: list[Case] = []
    seen: set[str] = set()
    for n, raw in enumerate(data.get("case") or [], start=1):
        where = f"case {n}" + (f" ({raw['id']!r})" if "id" in raw else "")
        allowed = set(Case.__dataclass_fields__)
        extra = set(raw) - allowed
        if extra:
            raise EvalFileError(f"{where}: unknown keys {sorted(extra)}")
        for key in ("id", "question", "expect"):
            if key not in raw:
                raise EvalFileError(f"{where}: {key} is required")
        if raw["id"] in seen:
            raise EvalFileError(f"{where}: the id is used twice")
        seen.add(raw["id"])
        expect = raw["expect"]
        if (
            not isinstance(expect, list)
            or not expect
            or not all(isinstance(i, int) for i in expect)
        ):
            raise EvalFileError(f"{where}: expect must be a non-empty list of item ids")
        kind = raw.get("kind", "search")
        if kind not in KINDS:
            raise EvalFileError(f"{where}: kind must be one of {', '.join(KINDS)}")
        if kind == "later" and not isinstance(raw.get("item"), int):
            raise EvalFileError(f"{where}: a later case needs item = <id of the old state>")
        cases.append(Case(**raw))
    if not cases:
        raise EvalFileError(f"{path}: no [[case]] entries")
    return cases, defaults


def run_cases(
    store: Store,
    config: Config,
    cases: list[Case],
    *,
    k: int = 5,
    sources: list[str] | None = None,
) -> EvalReport:
    """Run every case against the library. `sources` is the reader's scope (globs)."""
    from .server import later_items

    search = config.search
    results: list[CaseResult] = []
    for case in cases:
        scope = case.sources if case.sources is not None else sources
        allowed = store.resolve_sources(scope)
        missing = [i for i in case.expect if store.get(i) is None]
        # A copy of an expected item (the same session read from two sources) is the
        # same answer; search shows only one of them.
        valid = store.copies(case.expect)
        if case.kind == "later":
            start = store.get(case.item) if case.item is not None else None
            if start is None:
                missing.append(case.item or 0)
                got: list[int] = []
            else:
                newer, _ = later_items(
                    store,
                    start,
                    query=case.query,
                    allowed_sources=allowed,
                    limit=k,
                    candidate_pool=search.candidate_pool,
                )
                got = [i.id for i in newer]
        else:
            hits = store.search(
                case.query or case.question,
                allowed_sources=allowed,
                project=case.project,
                limit=k,
                candidate_pool=search.candidate_pool,
                recency_half_life_days=search.recency_half_life_days,
                recency_weight=search.recency_weight,
            )
            got = [h.item.id for h in hits]
        rank = next((n for n, i in enumerate(got, start=1) if i in valid), None)
        results.append(
            CaseResult(case.id, case.kind, rank is not None, rank, got, sorted(set(missing)))
        )
    total = len(results)
    ranks = [r.rank for r in results]
    return EvalReport(
        k=k,
        cases=total,
        passed=sum(r.passed for r in results),
        hit_at_1=round(sum(1 for r in ranks if r == 1) / total, 3),
        hit_at_k=round(sum(1 for r in ranks if r is not None) / total, 3),
        mrr=round(sum(1 / r for r in ranks if r) / total, 3),
        stale=sum(1 for r in results if r.missing),
        results=results,
    )


def compare(report: EvalReport, previous: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Case ids that started passing and that stopped passing since `previous`."""
    before = {r["id"]: r["passed"] for r in previous.get("results", [])}
    better = [r.id for r in report.results if r.passed and before.get(r.id) is False]
    worse = [r.id for r in report.results if not r.passed and before.get(r.id) is True]
    return better, worse


def format_report(report: EvalReport, cases: list[Case]) -> str:
    """Plain-text table: one line per case, then the totals."""
    by_id = {c.id: c for c in cases}
    lines = []
    width = max(len(r.id) for r in report.results)
    for r in report.results:
        mark = "ok  " if r.passed else "MISS"
        rank = f"#{r.rank}" if r.rank else "-"
        note = f"  stale key: {r.missing}" if r.missing else ""
        question = by_id[r.id].question
        if len(question) > 60:
            question = question[:57] + "..."
        lines.append(f"{mark} {r.id:<{width}}  {rank:>3}  {question}{note}")
    lines.append("")
    lines.append(
        f"{report.passed}/{report.cases} found in the top {report.k}"
        f"  (hit@1 {report.hit_at_1:.0%}, hit@{report.k} {report.hit_at_k:.0%},"
        f" MRR {report.mrr:.2f})"
    )
    if report.stale:
        lines.append(f"{report.stale} case(s) name items that are gone: update their expect")
    return "\n".join(lines)


def save(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=1) + "\n", encoding="utf-8")
