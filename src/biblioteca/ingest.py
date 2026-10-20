"""Turning raw records into stored items: redaction first, then validation, then upsert."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Atom, NewItem, Trust
from .redact import Redactor
from .store import Store


class IngestError(ValueError):
    pass


def item_from_record(
    record: Mapping[str, Any],
    *,
    source: str,
    redactor: Redactor,
    default_project: str | None = None,
    trust: Trust = "own",
) -> NewItem:
    """Build a `NewItem` from a pre-summarised record, redacting every text field."""

    def clean(value: str | None) -> str | None:
        return None if value is None else redactor.redact(str(value)).text

    try:
        occurred = datetime.fromisoformat(str(record["occurred_at"]))
        atoms = [Atom(kind=a["kind"], text=clean(a["text"]) or "") for a in record.get("atoms", [])]
        return NewItem(
            source=source,
            external_id=str(record["external_id"]),
            occurred_at=occurred,
            title=clean(record["title"]) or "",
            summary=clean(record["summary"]) or "",
            questions=[q for q in (clean(q) for q in record.get("questions", [])) if q],
            atoms=atoms,
            project=record.get("project") or default_project,
            account=record.get("account"),
            original=clean(record.get("original")),
            original_ref=record.get("original_ref"),
            trust=record.get("trust", trust),
            content_hash=str(record.get("content_hash", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IngestError(f"invalid record: {exc}") from exc


def import_jsonl(
    store: Store,
    path: Path,
    *,
    source: str,
    redactor: Redactor,
    default_project: str | None = None,
    trust: Trust = "own",
) -> Counter[str]:
    """Import pre-summarised items from a JSON Lines file. Returns outcome counts."""
    items: list[NewItem] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                items.append(
                    item_from_record(
                        record,
                        source=source,
                        redactor=redactor,
                        default_project=default_project,
                        trust=trust,
                    )
                )
            except (json.JSONDecodeError, IngestError) as exc:
                raise IngestError(f"{path}:{lineno}: {exc}") from exc
    return Counter(store.upsert_many(items))
