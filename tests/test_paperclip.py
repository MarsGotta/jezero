"""Paperclip connector against a mocked API."""

from __future__ import annotations

import json

import httpx
import pytest

from jezero.connectors.paperclip import PaperclipConnector
from jezero.store import ConnectorState

NOW = 2_000_000_000.0
OLD = "2033-05-18T00:00:00Z"  # well before NOW minus the settle window
CO = "co-1"


def api(issues: list[dict], comments: dict[str, list[dict]], docs=None, asks=None):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer k"
        path = request.url.path.removeprefix("/api")
        seen.append(path)
        if path == "/agents/me":
            return httpx.Response(200, json={"companyId": CO})
        if path == f"/companies/{CO}/issues":
            return httpx.Response(200, json=issues)
        if path == f"/companies/{CO}/agents":
            return httpx.Response(200, json=[{"id": "a1", "name": "Builder"}])
        if path == f"/companies/{CO}/projects":
            return httpx.Response(200, json=[{"id": "p1", "name": "Shop app"}])
        parts = path.split("/")
        issue = next(i for i in issues if i["id"] == parts[2])
        if len(parts) == 3:
            return httpx.Response(200, json=issue)
        table = {"comments": comments, "documents": docs or {}, "interactions": asks or {}}
        return httpx.Response(200, json=table[parts[3]].get(issue["id"], []))

    return httpx.MockTransport(handler), seen


def issue(iid: str, ident: str, **extra) -> dict:
    base = {
        "id": iid,
        "identifier": ident,
        "title": "Ship the checkout",
        "status": "done",
        "projectId": "p1",
        "assigneeAgentId": "a1",
        "createdByUserId": "u1",
        "createdAt": OLD,
        "updatedAt": OLD,
        "lastActivityAt": OLD,
        "description": "Move checkout to the new worker. " * 10,
    }
    base.update(extra)
    return base


def comment(cid: str, body: str, at: str = OLD, **extra) -> dict:
    return {"id": cid, "body": body, "createdAt": at, "authorAgentId": "a1", **extra}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("PC_KEY", "k")


def make(transport, **opts) -> PaperclipConnector:
    options = {"api_url": "http://pc.invalid/api", "api_key_env": "PC_KEY", **opts}
    return PaperclipConnector("pc", options, max_segment_chars=60_000, transport=transport)


def test_issue_becomes_one_doc_with_thread_documents_and_answers() -> None:
    transport, seen = api(
        [issue("i1", "ACME-1")],
        {
            "i1": [
                comment("c2", "Deployed to pre, PR #6 merged", at="2033-05-18T02:00:00Z"),
                comment("c1", "Plan: build.yml first", at="2033-05-18T01:00:00Z"),
                comment("c3", "Workspace ready", presentation={"kind": "system_notice"}),
                comment("c4", "removed", deletedAt=OLD),
                {
                    "id": "c5",
                    "body": "Approved, go ahead",
                    "createdAt": "2033-05-18T03:00:00Z",
                    "authorUserId": "u1",
                },
            ]
        },
        docs={
            "i1": [
                {
                    "id": "d1",
                    "key": "plan",
                    "title": "Plan",
                    "body": "1. build",
                    "latestRevisionNumber": 3,
                    "createdAt": OLD,
                    "updatedAt": OLD,
                }
            ]
        },
        asks={
            "i1": [
                {
                    "id": "q1",
                    "kind": "request_confirmation",
                    "title": "Merge?",
                    "status": "resolved",
                    "result": {"decision": "approve"},
                    "createdAt": OLD,
                    "createdByAgentId": "a1",
                }
            ]
        },
    )
    conn = make(transport, project_map={"Shop app": "shop"}, board_name="Owner")
    batch = conn.collect(ConnectorState(name="pc"), now=NOW)
    (doc,) = batch.docs
    assert doc.external_id == "ACME-1#0"
    assert doc.title_hint == "ACME-1 Ship the checkout"
    assert doc.project_hint == "shop"
    assert doc.original_ref == "paperclip-issue:co-1/ACME-1"
    text = doc.text
    assert "assignee: Builder" in text and "created by: Owner" in text
    assert text.index("Plan: build.yml") < text.index("PR #6") < text.index("Owner: Approved")
    assert "Workspace ready" not in text and "removed" not in text
    assert "document plan rev 3: Plan" in text
    assert "request_confirmation: Merge?" in text and "decision=approve" in text
    assert batch.next_cursor is not None
    assert all(not p.startswith("/companies/co-1/issues/") for p in seen)
    assert json.loads(json.dumps(doc.meta))["status"] == "done"


def test_recent_issues_are_deferred_and_cursor_skips_old_ones() -> None:
    fresh = "2033-05-18T03:30:00Z"  # NOW is 03:33:20 UTC on 2033-05-18
    transport, _ = api(
        [issue("i1", "ACME-1"), issue("i2", "ACME-2", updatedAt=fresh, lastActivityAt=fresh)],
        {"i1": [comment("c1", "done")], "i2": []},
    )
    conn = make(transport)
    batch = conn.collect(ConnectorState(name="pc"), now=NOW)
    assert [d.title_hint for d in batch.docs] == ["ACME-1 Ship the checkout"]
    assert batch.deferred == 1
    again = conn.collect(ConnectorState(name="pc", cursor=batch.next_cursor), now=NOW)
    assert again.docs == [] and again.deferred == 1


def test_missing_key_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("PC_KEY")
    transport, _ = api([], {})
    with pytest.raises(RuntimeError, match="PC_KEY"):
        make(transport).collect(ConnectorState(name="pc"), now=NOW)
