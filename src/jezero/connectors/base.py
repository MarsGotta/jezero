"""Connector building blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath
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


_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
_IGNORED_DIR_NAMES = {"projects", "worktrees", "src", "app"}


def project_from_cwd(cwd: str | None, aliases: dict[str, str] | None = None) -> str | None:
    """`.../projects/<name>/...` -> `<name>`, skipping ids and generic folder names.

    `aliases` maps folder names to project slugs (for example a worktrees folder).
    """
    if not cwd:
        return None
    parts = PurePath(cwd).parts
    for i, part in enumerate(parts[:-1]):
        if part == "projects":
            name = parts[i + 1]
            if not _NAME.match(name) or _UUIDISH.match(name) or name in _IGNORED_DIR_NAMES:
                continue
            name = name.lower()
            return (aliases or {}).get(name, name)
    return None


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
