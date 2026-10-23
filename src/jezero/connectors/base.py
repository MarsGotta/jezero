"""Connector building blocks."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..projects import ProjectRules
from ..store import ConnectorState

if TYPE_CHECKING:
    from ..config import ConnectorConfig


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
    """Short facts passed to the summarizer as context (`key=value`)."""
    session: str | None = None
    """Set by `whole_session` when the batch holds every segment of this session: ids are
    `<session>#<first block>`, and `session_segments` lists all of them."""
    session_segments: tuple[str, ...] = ()
    """With `session`: the complete set of segment ids. Stored items of the same source
    with the `<session>#` prefix and an id not listed are stale and may be pruned."""


def whole_session(session: str, docs: list[RawDoc]) -> list[RawDoc]:
    """Mark `docs` as every segment `session` has now (see `RawDoc.session`)."""
    ids = tuple(d.external_id for d in docs)
    for doc in docs:
        doc.session = session
        doc.session_segments = ids
    return docs


def require_dir(option: str, path: Path) -> None:
    """A configured folder that is missing fails the run with a message naming it."""
    if not path.is_dir():
        raise FileNotFoundError(f"{option} folder not found: {path}")


def glob_base(pattern: str) -> Path:
    """The folder a glob starts from: its parts before the first wildcard."""
    import glob

    fixed = []
    for part in Path(pattern).parts:
        if glob.has_magic(part):
            break
        fixed.append(part)
    return Path(*fixed) if fixed else Path()


def require_file(option: str, path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{option} file not found: {path}")


@dataclass(slots=True)
class Batch:
    docs: list[RawDoc]
    next_cursor: str | None
    """Cursor to persist once every doc in the batch has been stored."""
    deferred: int = 0
    """Units seen but postponed (for example sessions still active)."""
    warnings: list[str] = field(default_factory=list)
    """Problems that must reach the watchdog, such as a source format the connector no
    longer understands. The pipeline records them as the connector's error and keeps the
    cursor where it was, so the alert stays until the cause is fixed."""
    skipped: list[str] = field(default_factory=list)
    """Units that could not be read at all (a malformed file) and were left out. They are
    reported as the run's error, but the cursor moves past them: reading the same bytes
    again cannot succeed, and holding the cursor would re-read every newer unit forever."""


TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (OSError, sqlite3.DatabaseError, MemoryError)
"""Errors that may go away on the next run: they fail the run instead of skipping the unit,
so the cursor stays and the unit is read again. That covers a network folder, a locked
database, and a damaged one (`sqlite3.DatabaseError`, the parent of `OperationalError`:
`database disk image is malformed` is the store, not the session, and the session must be
read again once the file is repaired), and running out of memory."""


def read_or_skip[T](
    label: str,
    skipped: list[str],
    read: Callable[[], T],
    *,
    transient: tuple[type[BaseException], ...] = (),
) -> T | None:
    """Run `read` for one unit (a session, a file, an issue). When the unit itself is
    malformed, note it in `skipped` and return None, so one bad unit never blocks the rest
    of the source. Transient errors are raised as before."""
    try:
        return read()
    except TRANSIENT_ERRORS + transient:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse error of one unit
        skipped.append(f"skipped {label}: {type(exc).__name__}: {exc}"[:300])
        return None


def batch_full(
    taken: int, stamp: float, last: float | None, *, limit: int | None, sample: int | None
) -> bool:
    """Whether a collect loop must stop before the next unit (timestamp `stamp`).

    `limit` never cuts inside a group of units with the same timestamp: the cursor becomes
    the newest timestamp processed and the next run reads strictly newer units, so the rest
    of the group would be skipped for good. `last` is the timestamp of the last unit taken.
    """
    if sample is not None:
        return taken >= sample
    if limit is None or taken < limit:
        return False
    return last is None or stamp != last


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


@dataclass(frozen=True, slots=True)
class BuildContext:
    """What a connector may need from the global config to be built."""

    max_segment_chars: int
    project_rules: ProjectRules


Status = Literal["available", "planned"]


@dataclass(frozen=True, slots=True)
class ConnectorType:
    """A connector type, as declared by the module that implements it.

    `build` creates a connector from its `[[connectors]]` entry. Types without `build`
    are listed (so `check-config` can tell a typo from a planned type) but never run on a
    schedule.
    """

    name: str
    status: Status
    description: str
    default_trust: Literal["own", "external"] = "own"
    build: Callable[[ConnectorConfig, BuildContext], Connector] | None = None


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


READ_ONLY_COMMAND = re.compile(
    r"^\s*(sed -n|rg|grep|cat|head|tail|ls|nl|wc|find|pwd|tree|stat|file|which|"
    r"git (status|diff|log|show|branch|rev-parse|ls-files))\b"
)
"""Shell commands that only inspect; connectors drop them unless asked to keep them."""


class PromptCleaner:
    """Strip instructions an orchestrator repeats at the top of every prompt.

    Options: `user_prompt_markers` (keep a message only from the first marker on) and
    `user_prompt_drop_lines` (drop fixed procedure lines that remain). Both are regexes.
    """

    def __init__(self, options: dict[str, Any]) -> None:
        self.markers = [re.compile(m, re.MULTILINE) for m in options.get("user_prompt_markers", [])]
        drop = options.get("user_prompt_drop_lines", [])
        self.drop = re.compile("|".join(f"(?:{d})" for d in drop)) if drop else None

    def __bool__(self) -> bool:
        return bool(self.markers or self.drop)

    def clean(self, text: str) -> str:
        for marker in self.markers:
            found = marker.search(text)
            if found:
                text = text[found.start() :]
                break
        if self.drop:
            drop = self.drop
            text = "\n".join(line for line in text.splitlines() if not drop.match(line))
        return text
