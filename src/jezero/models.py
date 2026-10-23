"""Core data types shared by the store, connectors and the MCP server."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

AtomKind = Literal["decision", "rationale", "fix", "error", "command", "fact", "risk"]
ATOM_KINDS: tuple[str, ...] = ("decision", "rationale", "fix", "error", "command", "fact", "risk")

Trust = Literal["own", "external"]
"""`own`: written by the owner or their agents. `external`: written by third parties (e.g. email).

External content is untrusted data and must never be treated as instructions by a consumer.
"""


@dataclass(frozen=True, slots=True)
class Atom:
    """A small, exact, reusable piece of knowledge extracted from an item."""

    kind: AtomKind
    text: str

    def __post_init__(self) -> None:
        if self.kind not in ATOM_KINDS:
            raise ValueError(f"unknown atom kind {self.kind!r}; expected one of {ATOM_KINDS}")
        if not self.text.strip():
            raise ValueError("atom text must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


def utc(dt: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC. Naive values are rejected on purpose."""
    if dt.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return dt.astimezone(UTC)


@dataclass(slots=True)
class NewItem:
    """An item ready to be stored: one agent session, one email thread, one document."""

    source: str
    """Name of the connector instance that produced it (from the config)."""
    external_id: str
    """Stable id inside the source (session id, thread id). Unique per source."""
    occurred_at: datetime
    """When it actually happened, not when it was ingested."""
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    atoms: list[Atom] = field(default_factory=list)
    project: str | None = None
    account: str | None = None
    original: str | None = None
    """Full (redacted) original text, if it is stored inline."""
    original_ref: str | None = None
    """Pointer to the original (path, URL) when it is not stored inline."""
    trust: Trust = "own"
    content_hash: str = ""
    """Hash of the redacted source text the summary was made from (what the pipeline
    compares). Unchanged hash means nothing to re-summarise."""

    def __post_init__(self) -> None:
        self.occurred_at = utc(self.occurred_at)
        for name in ("source", "external_id", "title", "summary"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if self.trust not in ("own", "external"):
            raise ValueError("trust must be 'own' or 'external'")
        if not self.content_hash:
            self.content_hash = content_hash(self.original or self.summary)


@dataclass(slots=True)
class Item(NewItem):
    id: int = 0
    ingested_at: datetime | None = None

    def to_dict(self, *, include_original: bool = False) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "source": self.source,
            "external_id": self.external_id,
            "occurred_at": self.occurred_at.isoformat(),
            "title": self.title,
            "summary": self.summary,
            "questions": list(self.questions),
            "atoms": [a.to_dict() for a in self.atoms],
            "project": self.project,
            "account": self.account,
            "original_ref": self.original_ref,
            "trust": self.trust,
        }
        if include_original:
            data["original"] = self.original
        return data


@dataclass(frozen=True, slots=True)
class SearchHit:
    item: Item
    score: float
    """Final score: BM25 relevance adjusted by recency. Higher is better."""
    relevance: float
    """BM25 relevance alone (sign flipped so higher is better)."""


def content_hash(text: str) -> str:
    # `surrogatepass`: a lone surrogate (half an emoji, JSON-escaped) must not make the
    # hash raise. Valid text hashes exactly as before.
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# Terminal control sequences: OSC (title, clipboard `OSC 52`, hyperlinks `OSC 8`), DCS, SOS,
# PM and APC strings up to their terminator (BEL or ST); CSI sequences (colours, cursor
# moves, `ESC [ 2 J`); other ESC sequences; and their 8-bit C1 forms. Each class excludes its
# own terminators, so a scan never backtracks.
_CONTROL_STRING = re.compile(
    r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
_ESCAPE = re.compile(r"\x1b[\x20-\x2f]*[\x30-\x7e]?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_display(text: str) -> str:
    """`text` without terminal control characters: C0 controls except newline and tab, DEL
    and C1 (0x80-0x9f). Escape sequences go whole (`ESC ] 52 ; c ; ... BEL` leaves nothing),
    so a stored title or summary cannot set the clipboard, open links or move the cursor of
    the terminal that prints it."""
    if not _CONTROL.search(text):
        return text
    text = _CONTROL_STRING.sub("", text)
    text = _CSI.sub("", text)
    text = _ESCAPE.sub("", text)
    return _CONTROL.sub("", text)


def clean_tree[T](value: T) -> T:
    """`clean_display` on every string of a JSON-like value (dicts, lists, tuples)."""
    if isinstance(value, str):
        return clean_display(value)  # type: ignore[return-value]
    if isinstance(value, dict):
        return {k: clean_tree(v) for k, v in value.items()}  # type: ignore[return-value]
    if isinstance(value, list | tuple):
        return type(value)(clean_tree(v) for v in value)  # type: ignore[return-value]
    return value


def atoms_from_json(raw: str | None) -> list[Atom]:
    if not raw:
        return []
    return [Atom(kind=a["kind"], text=a["text"]) for a in json.loads(raw)]
