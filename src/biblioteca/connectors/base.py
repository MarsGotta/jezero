"""Connector building blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..store import ConnectorState


@dataclass(slots=True)
class RawDoc:
    """One unit to summarise: a session (or a part of a long one), a thread, a document."""

    external_id: str
    occurred_at: datetime
    text: str
    title_hint: str | None = None
    project_hint: str | None = None
    original_ref: str | None = None
    activity_key: str | None = None
    """Opaque cursor value; the pipeline stores the last one it fully processed."""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Batch:
    docs: list[RawDoc]
    next_cursor: str | None
    """Cursor to persist once every doc in the batch has been stored."""
    deferred: int = 0
    """Units seen but postponed (for example sessions still active)."""


class Connector(Protocol):
    name: str

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
    ) -> Batch:
        """Return the docs to (re)process. `sample` picks the newest N without a cursor."""
        ...


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(limit - 40, 0) // 2
    return f"{text[:keep]}\n[... {len(text) - 2 * keep} characters omitted ...]\n{text[-keep:]}"


def segment(
    blocks: list[tuple[int, float, str]], max_chars: int
) -> list[list[tuple[int, float, str]]]:
    """Group (id, timestamp, text) blocks into consecutive segments under `max_chars`.

    Boundaries depend only on the blocks themselves, so earlier segments stay identical
    when new blocks are appended: only the last segment changes as a session grows.
    """
    segments: list[list[tuple[int, float, str]]] = []
    current: list[tuple[int, float, str]] = []
    size = 0
    for block in blocks:
        text = truncate_middle(block[2], max_chars)
        block = (block[0], block[1], text)
        if current and size + len(text) > max_chars:
            segments.append(current)
            current, size = [], 0
        current.append(block)
        size += len(text) + 1
    if current:
        segments.append(current)
    return segments
