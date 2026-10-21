from __future__ import annotations

import sqlite3

import pytest

from jezero.models import Atom
from jezero.store import Store, StoreError, build_match_query

from .conftest import NOW, make_item


def search(store: Store, query: str, **kw):
    return store.search(query, now=NOW, **kw)


def test_init_is_idempotent(store: Store) -> None:
    store.init()
    assert store.stats() == {"items": 0, "sources": {}}


def test_tokenizer_change_is_refused(tmp_path) -> None:
    Store(tmp_path / "x.db").init()
    with pytest.raises(StoreError, match="tokenizer"):
        Store(tmp_path / "x.db", tokenizer="unicode61 remove_diacritics 2").init()


def test_upsert_inserted_unchanged_updated(store: Store) -> None:
    item = make_item("s1", "Tunnel setup", "Configured the embeddings tunnel.")
    assert store.upsert(item) == "inserted"
    assert store.upsert(item) == "unchanged"
    changed = make_item("s1", "Tunnel setup", "Configured the embeddings tunnel again.")
    assert store.upsert(changed) == "updated"
    assert store.stats()["items"] == 1
    hits = search(store, "again")
    assert [h.item.external_id for h in hits] == ["s1"]


def test_accents_and_plurals_match(store: Store) -> None:
    store.upsert(make_item("a", "Configuración del túnel", "Revisamos los correos pendientes."))
    assert search(store, "configuracion")
    assert search(store, "tunel")
    assert search(store, "correo")  # substring of "correos"


def test_phrase_query(store: Store) -> None:
    store.upsert(make_item("a", "Mail", "el correo de la empresa queda pendiente"))
    store.upsert(make_item("b", "Mail", "pendiente queda el correo"))
    hits = search(store, '"correo de la empresa"')
    assert [h.item.external_id for h in hits] == ["a"]


def test_atoms_and_questions_are_searchable(store: Store) -> None:
    store.upsert(
        make_item(
            "a",
            "Session",
            "Generic summary.",
            atoms=[Atom("error", "ModuleNotFoundError: icalendar")],
            questions=["Why did the calendar script fail?"],
        )
    )
    assert search(store, "icalendar")
    assert search(store, "calendar script")


def test_recency_breaks_ties(store: Store) -> None:
    store.upsert(make_item("old", "Deploy notes", "deploy rollback procedure", days_ago=400))
    store.upsert(make_item("new", "Deploy notes", "deploy rollback procedure", days_ago=1))
    hits = search(store, "rollback")
    assert [h.item.external_id for h in hits] == ["new", "old"]
    assert hits[0].relevance == pytest.approx(hits[1].relevance)


def test_source_restriction(store: Store) -> None:
    store.upsert(make_item("a", "Agent work", "shared keyword here", source="agent-x"))
    store.upsert(make_item("m", "Mail thread", "shared keyword here", source="mail-personal"))
    hits = search(store, "keyword", allowed_sources=["agent-*"])
    assert {h.item.source for h in hits} == {"agent-x"}
    assert search(store, "keyword", allowed_sources=["nothing-*"]) == []
    # an explicit source outside the allowed set returns nothing
    assert search(store, "keyword", allowed_sources=["agent-*"], source="mail-personal") == []


def test_get_respects_sources(store: Store) -> None:
    store.upsert(make_item("m", "Mail", "private", source="mail-personal"))
    item_id = search(store, "private")[0].item.id
    assert store.get(item_id) is not None
    assert store.get(item_id, allowed_sources=["agent-*"]) is None


def test_timeline_order_and_filters(store: Store) -> None:
    store.upsert(make_item("1", "One", "first", days_ago=3, project="p1"))
    store.upsert(make_item("2", "Two", "second", days_ago=2, project="p2"))
    store.upsert(make_item("3", "Three", "third", days_ago=1, project="p1"))
    assert [i.external_id for i in store.timeline()] == ["3", "2", "1"]
    assert [i.external_id for i in store.timeline(project="p1")] == ["3", "1"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("hello world", '"hello" OR "world"'),
        ('"exact phrase" word', '"exact phrase" OR "word"'),
        ("ab", None),  # too short for trigram
        ('NEAR( x" OR *', '"NEAR("'),  # operators are neutralised, short terms dropped
    ],
)
def test_build_match_query(query: str, expected: str | None) -> None:
    assert build_match_query(query) == expected


def test_hostile_query_never_errors(store: Store) -> None:
    store.upsert(make_item("a", "Title", "content"))
    for q in ['"', "AND OR NOT", "col:title", "^start", "a*b*", "((((", "content)"]:
        search(store, q)  # must not raise sqlite3.OperationalError


def test_readonly_connection_cannot_write(store: Store) -> None:
    con = store.connect(readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM items")
    con.close()


def test_set_project_updates_item_and_search_filter(store: Store) -> None:
    store.upsert_many([make_item("e1", "Supervised tasks", "backoff for the hub worker")])
    assert store.project_of("agent-test", "e1") == "demo"
    assert store.set_project("agent-test", "e1", "atlas") is True
    assert store.set_project("agent-test", "e1", "atlas") is False
    assert store.set_project("agent-test", "missing", "atlas") is False
    assert store.project_of("agent-test", "e1") == "atlas"
    assert [h.item.project for h in search(store, "backoff", project="atlas")] == ["atlas"]


def test_redact_stored_rewrites_items_and_index(tmp_path) -> None:
    from jezero.redact import Redactor

    store = Store(tmp_path / "lib.db")
    store.init()
    store.upsert(make_item("r1", "Deploy notes", "export GITHUB_TOKEN=abcd1234efgh5678ijkl ran"))
    redactor = Redactor()

    def redact(text: str) -> tuple[str, int]:
        result = redactor.redact(text)
        return result.text, result.count

    assert store.redact_stored(redact, dry_run=True) == (1, 1)
    assert store.search("abcd1234efgh5678ijkl")
    assert store.redact_stored(redact, dry_run=False) == (1, 1)
    assert not store.search("abcd1234efgh5678ijkl")
    assert store.search("Deploy notes")
    assert store.redact_stored(redact, dry_run=False) == (0, 0)
