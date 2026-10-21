"""Paperclip connector: issues with their comments, documents and decisions.

Paperclip is an orchestrator for teams of agents. Its issues hold what agent sessions
only show in fragments: what was asked, the plan, each agent's report and the board's
answers. Every issue becomes one or more docs, in this order:

- a header (identifier, title, status, project, assignee) and the description;
- comments, oldest first, with the author's name;
- issue documents (plans, runbooks) at the time of their last revision;
- interactions (questions, confirmations) with their answer.

The connector only sends GET requests. The API key is read from an env var
(`api_key_env`); an agent key scoped to one company is enough.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from ..store import ConnectorState
from .base import Batch, BuildContext, ConnectorType, RawDoc, segment, truncate_middle

if TYPE_CHECKING:
    from ..config import ConnectorConfig


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M")


class PaperclipConnector:
    def __init__(
        self,
        name: str,
        options: dict[str, Any],
        *,
        max_segment_chars: int,
        default_project: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.api_url = str(options.get("api_url", "http://127.0.0.1:3100/api")).rstrip("/")
        self.api_key_env = str(options.get("api_key_env", "PAPERCLIP_API_KEY"))
        self.company_ids = [str(c) for c in options.get("company_ids", [])]
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.max_block_chars = int(options.get("max_block_chars", 8000))
        self.include_documents = bool(options.get("include_documents", True))
        self.include_interactions = bool(options.get("include_interactions", True))
        self.skip_system_notices = bool(options.get("skip_system_notices", True))
        self.skip_statuses = set(options.get("skip_statuses", []))
        self.project_map = {str(k): str(v) for k, v in options.get("project_map", {}).items()}
        self.board_name = str(options.get("board_name", "board"))
        self.min_chars = int(options.get("min_chars", 200))
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project
        self.transport = transport

    # -- HTTP -------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"env var {self.api_key_env} is not set")
        return httpx.Client(
            base_url=self.api_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
            transport=self.transport,
        )

    @staticmethod
    def _get(http: httpx.Client, path: str) -> Any:
        resp = http.get(path)
        resp.raise_for_status()
        return resp.json()

    # -- collection ---------------------------------------------------------------------

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
        now: float | None = None,
    ) -> Batch:
        now = time.time() if now is None else now
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        with self._client() as http:
            companies = self.company_ids or [str(self._get(http, "/agents/me")["companyId"])]
            candidates: list[tuple[float, str, dict[str, Any]]] = []
            for company in companies:
                for issue in self._get(http, f"/companies/{company}/issues?limit=10000"):
                    if issue.get("hiddenAt") or issue.get("status") in self.skip_statuses:
                        continue
                    activity = max(
                        _ts(issue.get("lastActivityAt")) or 0.0,
                        _ts(issue.get("updatedAt")) or 0.0,
                    )
                    if activity > cursor:
                        candidates.append((activity, company, issue))
            deferred = sum(1 for a, _, _ in candidates if a > settled_before)
            ready = sorted(
                (c for c in candidates if c[0] <= settled_before),
                key=lambda c: c[0],
                reverse=sample is not None,
            )
            names: dict[str, dict[str, str]] = {}
            docs: list[RawDoc] = []
            processed: list[float] = []
            taken = 0
            for activity, company, issue in ready:
                if (sample is not None and taken >= sample) or (
                    limit is not None and taken >= limit
                ):
                    break
                if company not in names:
                    names[company] = self._names(http, company)
                issue_docs = self._issue_docs(http, issue, names[company])
                processed.append(activity)
                docs.extend(issue_docs)
                taken += 1 if (issue_docs or sample is None) else 0
        next_cursor = repr(max(processed)) if sample is None and processed else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _names(self, http: httpx.Client, company: str) -> dict[str, str]:
        names: dict[str, str] = {}
        for agent in self._get(http, f"/companies/{company}/agents"):
            names[str(agent["id"])] = str(agent.get("name") or agent["id"])
        try:
            for project in self._get(http, f"/companies/{company}/projects"):
                names[f"project:{project['id']}"] = str(project.get("name") or "")
        except httpx.HTTPStatusError:
            pass
        return names

    def _author(self, record: dict[str, Any], names: dict[str, str]) -> str:
        agent = record.get("authorAgentId") or record.get("createdByAgentId")
        if agent:
            return names.get(str(agent), "agent")
        if record.get("authorUserId") or record.get("createdByUserId"):
            return self.board_name
        return "system"

    def _issue_docs(
        self, http: httpx.Client, issue: dict[str, Any], names: dict[str, str]
    ) -> list[RawDoc]:
        issue_id = str(issue["id"])
        detail = self._get(http, f"/issues/{issue_id}")
        ident = str(detail.get("identifier") or issue_id)
        title = str(detail.get("title") or "")
        project_name = names.get(f"project:{detail.get('projectId')}", "")
        project = self.project_map.get(project_name) or self.default_project
        assignee = names.get(str(detail.get("assigneeAgentId")), "") or (
            self.board_name if detail.get("assigneeUserId") else "unassigned"
        )
        created = _ts(detail.get("createdAt")) or time.time()

        header = (
            f"[{_when(created)}] issue {ident}: {title}\n"
            f"status: {detail.get('status')} | project: {project_name or '-'} | "
            f"assignee: {assignee} | created by: {self._author(detail, names)}"
        )
        description = (detail.get("description") or "").strip()
        if description:
            header += "\n\n" + truncate_middle(description, self.max_block_chars)
        # Everything after the header is ordered by creation time, so new comments land at
        # the end and earlier segments (and their ids) stay put as the issue grows. A
        # document edited in place keeps its slot; only its segment is summarised again.
        entries: list[tuple[float, str, float, str]] = []  # (created, id, shown at, text)

        comments = self._get(http, f"/issues/{issue_id}/comments")
        comments.sort(key=lambda c: (_ts(c.get("createdAt")) or 0.0, str(c.get("id"))))
        for comment in comments:
            if comment.get("deletedAt"):
                continue
            presentation = comment.get("presentation") or {}
            if self.skip_system_notices and presentation.get("kind") == "system_notice":
                continue
            body = (comment.get("body") or "").strip()
            if not body:
                continue
            stamp = _ts(comment.get("createdAt")) or created
            text = truncate_middle(body, self.max_block_chars)
            line = f"[{_when(stamp)}] {self._author(comment, names)}: {text}"
            entries.append((stamp, str(comment.get("id")), stamp, line))

        if self.include_interactions:
            for item in self._get(http, f"/issues/{issue_id}/interactions"):
                stamp = _ts(item.get("createdAt")) or created
                parts = [f"{item.get('kind')}: {item.get('title') or ''}".strip()]
                if item.get("summary"):
                    parts.append(str(item["summary"]))
                parts.append(f"status: {item.get('status')}")
                if item.get("result"):
                    parts.append(f"answer: {_compact(item['result'])}")
                text = truncate_middle("\n".join(parts), self.max_block_chars)
                who = self._author(item, names)
                line = f"[{_when(stamp)}] {who} asks {text}"
                entries.append((stamp, str(item.get("id")), stamp, line))

        if self.include_documents:
            for document in self._get(http, f"/issues/{issue_id}/documents"):
                body = (document.get("body") or "").strip()
                if not body:
                    continue
                born = _ts(document.get("createdAt")) or created
                stamp = _ts(document.get("updatedAt")) or born
                head = (
                    f"[{_when(stamp)}] document {document.get('key')} "
                    f"rev {document.get('latestRevisionNumber')}: {document.get('title') or ''}"
                )
                text = truncate_middle(body, self.max_block_chars)
                entries.append((born, str(document.get("id")), stamp, f"{head}\n{text}"))

        entries.sort(key=lambda e: (e[0], e[1]))
        blocks: list[tuple[int, float, str]] = [(0, created, header)]
        blocks += [(n, e[2], e[3]) for n, e in enumerate(entries, 1)]
        if sum(len(b[2]) for b in blocks) < self.min_chars:
            return []
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for number, part in enumerate(parts, 1):
            label = f"{ident} {title}"
            if len(parts) > 1:
                label += f" (part {number}/{len(parts)})"
            docs.append(
                RawDoc(
                    external_id=f"{ident}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(max(b[1] for b in part), UTC),
                    text="\n\n".join(b[2] for b in part),
                    title_hint=label,
                    project_hint=project,
                    original_ref=f"paperclip-issue:{ident}",
                    meta={"status": detail.get("status"), "part": number, "parts": len(parts)},
                )
            )
        return docs


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k}={_compact(v)}" for k, v in value.items() if v not in (None, "", []))
    if isinstance(value, list):
        return ", ".join(_compact(v) for v in value)
    return str(value)


def _build(conn: ConnectorConfig, ctx: BuildContext) -> PaperclipConnector:
    return PaperclipConnector(
        conn.name,
        conn.options,
        max_segment_chars=ctx.max_segment_chars,
        default_project=conn.project,
    )


CONNECTOR_TYPE = ConnectorType(
    "paperclip",
    "available",
    "Paperclip issues with comments, documents and decisions (read-only API).",
    build=_build,
)
