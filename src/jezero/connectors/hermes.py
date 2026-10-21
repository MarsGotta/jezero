"""Hermes Agent sessions, read straight from its SQLite state database (read-only), or
from the session exports the Jezero Hermes plugin uploads from other computers.

Each session becomes one or more docs: the user/assistant dialogue (tool *results* are
skipped, they are mostly noise; tool *calls* are kept as one short line) split into
segments under the summarizer's input budget. Sessions still active are deferred until
they have been quiet for `settle_minutes`, so an ongoing conversation is not summarised
every few minutes. The project comes from the session's working folder (`cwd`), with the
same rules as the other agent connectors.

Options: `state_db` (a profile's state.db) or `export_dir` (a device's inbox folder of
`<session>.json` exports), exactly one of them.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..projects import DEFAULT_RULES, ProjectRules
from ..store import ConnectorState
from .base import Batch, BuildContext, ConnectorType, RawDoc, segment

if TYPE_CHECKING:
    from ..config import ConnectorConfig

_INJECTED = re.compile(
    r"<(memory-context|system-reminder)>.*?</\1>|\[System note:.*?\]\s*", re.DOTALL
)


class HermesConnector:
    def __init__(
        self,
        name: str,
        options: dict[str, Any],
        *,
        max_segment_chars: int,
        default_project: str | None = None,
        project_rules: ProjectRules | None = None,
    ) -> None:
        self.name = name
        state_db, export_dir = options.get("state_db"), options.get("export_dir")
        if bool(state_db) == bool(export_dir):
            raise ValueError("hermes connector: set exactly one of state_db or export_dir")
        self.state_db = Path(os.path.expanduser(str(state_db))) if state_db else None
        self.export_dir = Path(os.path.expanduser(str(export_dir))) if export_dir else None
        self.rules = (project_rules or DEFAULT_RULES).with_aliases(
            dict(options.get("project_aliases", {}))
        )
        self.skip_sources = set(options.get("skip_session_sources", ["cron", "subagent"]))
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    def _connect(self) -> sqlite3.Connection:
        assert self.state_db is not None
        if not self.state_db.is_file():
            raise FileNotFoundError(f"Hermes state database not found: {self.state_db}")
        con = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
        now: float | None = None,
    ) -> Batch:
        now = time.time() if now is None else now
        if self.export_dir is not None:
            return self._collect_exports(state, limit=limit, sample=sample, now=now)
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        placeholders = ",".join("?" * len(self.skip_sources)) or "''"
        with closing(self._connect()) as con:
            has_cwd = "cwd" in {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            cwd = "s.cwd" if has_cwd else "NULL"
            sessions = con.execute(
                f"SELECT s.id, s.source, s.title, s.started_at, {cwd} AS cwd, "  # noqa: S608
                "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) AS activity "
                f"FROM sessions s WHERE s.source NOT IN ({placeholders})",
                list(self.skip_sources),
            ).fetchall()
            candidates = [s for s in sessions if s["activity"] and s["activity"] > cursor]
            deferred = sum(1 for s in candidates if s["activity"] > settled_before)
            ready = sorted(
                (s for s in candidates if s["activity"] <= settled_before),
                key=lambda s: s["activity"],
                reverse=sample is not None,
            )
            if sample is not None:
                ready = ready[:sample]
            elif limit is not None:
                ready = ready[:limit]
            docs: list[RawDoc] = []
            for session in ready:
                docs.extend(self._session_docs(dict(session), self._rows(con, session["id"])))
        next_cursor = None
        if sample is None and ready:
            next_cursor = repr(max(s["activity"] for s in ready))
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _collect_exports(
        self, state: ConnectorState, *, limit: int | None, sample: int | None, now: float
    ) -> Batch:
        """Exports uploaded by a device: one JSON file per session, newest upload wins.
        The file's mtime (the upload time) is the cursor, like the other inbox readers."""
        assert self.export_dir is not None
        if not self.export_dir.is_dir():
            return Batch(docs=[], next_cursor=None, deferred=0)
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        candidates = []
        for path in self.export_dir.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime > cursor:
                candidates.append((mtime, path))
        deferred = sum(1 for m, _ in candidates if m > settled_before)
        ready = sorted(
            (c for c in candidates if c[0] <= settled_before), reverse=sample is not None
        )
        if sample is not None:
            ready = ready[:sample]
        elif limit is not None:
            ready = ready[:limit]
        docs: list[RawDoc] = []
        for _, path in ready:
            try:
                export = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            session = export.get("session") if isinstance(export, dict) else None
            messages = export.get("messages") if isinstance(export, dict) else None
            if not isinstance(session, dict) or not isinstance(messages, list):
                continue
            if session.get("source") in self.skip_sources:
                continue
            session = {"id": path.stem, **session}
            rows = [m for m in messages if isinstance(m, dict) and m.get("role")]
            docs.extend(self._session_docs(session, rows, session_file=path))
        next_cursor = repr(max(m for m, _ in ready)) if sample is None and ready else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _rows(self, con: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
        columns = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        active = " AND active = 1" if "active" in columns else ""
        return con.execute(
            "SELECT id, role, content, tool_calls, timestamp FROM messages "  # noqa: S608
            f"WHERE session_id = ? AND role IN ('user', 'assistant'){active} ORDER BY id",
            (session_id,),
        ).fetchall()

    def _session_docs(
        self,
        session: Mapping[str, Any],
        rows: list[Any],
        session_file: Path | None = None,
    ) -> list[RawDoc]:
        project = (
            self.rules.project_for(session.get("cwd"), session_file=session_file)
            or self.default_project
        )
        blocks: list[tuple[int, float, str]] = []
        for row in rows:
            text = self._render(row)
            if text:
                stamp = row["timestamp"] or session.get("started_at") or 0
                blocks.append((row["id"], float(stamp), text))
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for index, part in enumerate(parts, 1):
            title = session.get("title") or None
            if title and len(parts) > 1:
                title = f"{title} (part {index}/{len(parts)})"
            docs.append(
                RawDoc(
                    external_id=f"{session['id']}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(part[-1][1], UTC),
                    text="\n".join(b[2] for b in part),
                    title_hint=title,
                    project_hint=project,
                    original_ref=f"hermes-session:{session['id']}",
                    meta={"channel": session.get("source"), "part": index, "parts": len(parts)},
                )
            )
        return docs

    def _render(self, row: Mapping[str, Any]) -> str:
        if row["role"] not in ("user", "assistant"):
            return ""
        stamp = datetime.fromtimestamp(float(row["timestamp"] or 0), UTC).strftime("%Y-%m-%d %H:%M")
        content = _INJECTED.sub("", row["content"] or "").strip()
        lines = [f"[{stamp}] {row['role']}: {content}"] if content else []
        if self.include_tool_calls and row["role"] == "assistant" and row["tool_calls"]:
            lines += [f"[{stamp}] tool call: {c}" for c in self._tool_calls(row["tool_calls"])]
        return "\n".join(lines)

    def _tool_calls(self, raw: str) -> list[str]:
        try:
            calls = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        rendered = []
        for call in calls if isinstance(calls, list) else []:
            fn = call.get("function", call) if isinstance(call, dict) else {}
            name = fn.get("name", "?")
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            if len(args) > self.max_tool_args:
                args = args[: self.max_tool_args] + "..."
            rendered.append(f"{name} {args}")
        return rendered


def _build(conn: ConnectorConfig, ctx: BuildContext) -> HermesConnector:
    return HermesConnector(
        conn.name,
        conn.options,
        max_segment_chars=ctx.max_segment_chars,
        default_project=conn.project,
        project_rules=ctx.project_rules,
    )


CONNECTOR_TYPE = ConnectorType(
    "hermes",
    "available",
    "Hermes Agent sessions from its state.db (read-only) or from device uploads.",
    build=_build,
)
