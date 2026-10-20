from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from biblioteca.models import Atom, NewItem
from biblioteca.store import Store

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def make_item(
    external_id: str,
    title: str,
    summary: str,
    *,
    source: str = "agent-test",
    days_ago: float = 0,
    project: str | None = "demo",
    atoms: list[Atom] | None = None,
    questions: list[str] | None = None,
    trust: str = "own",
) -> NewItem:
    return NewItem(
        source=source,
        external_id=external_id,
        occurred_at=NOW - timedelta(days=days_ago),
        title=title,
        summary=summary,
        questions=questions or [],
        atoms=atoms or [],
        project=project,
        trust=trust,  # type: ignore[arg-type]
    )


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "lib.db")
    s.init()
    return s
