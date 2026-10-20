"""Hermes Agent sessions, read straight from its SQLite state database (read-only).

Each session becomes one or more docs: the user/assistant dialogue (tool *results* are
skipped, they are mostly noise; tool *calls* are kept as one short line) split into
segments under the summarizer's input budget. Sessions still active are deferred until
they have been quiet for `settle_minutes`, so an ongoing conversation is not summarised
every few minutes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..store import ConnectorState
from .base import Batch, RawDoc, segment

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
    ) -> None:
        self.name = name
        self.state_db = Path(str(options["state_db"]))
        self.skip_sources = set(options.get("skip_session_sources", ["cron", "subagent"]))
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    def _connect(self) -> sqlite3.Connection:
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
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        placeholders = ",".join("?" * len(self.skip_sources)) or "''"
        with closing(self._connect()) as con:
            sessions = con.execute(
                "SELECT s.id, s.source, s.title, s.started_at, "  # noqa: S608
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
                docs.extend(self._session_docs(con, session))
        next_cursor = None
        if sample is None and ready:
            next_cursor = repr(max(s["activity"] for s in ready))
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _session_docs(self, con: sqlite3.Connection, session: sqlite3.Row) -> list[RawDoc]:
        columns = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        active = " AND active = 1" if "active" in columns else ""
        rows = con.execute(
            "SELECT id, role, content, tool_calls, timestamp FROM messages "  # noqa: S608
            f"WHERE session_id = ? AND role IN ('user', 'assistant'){active} ORDER BY id",
            (session["id"],),
        ).fetchall()
        blocks: list[tuple[int, float, str]] = []
        for row in rows:
            text = self._render(row)
            if text:
                blocks.append((row["id"], row["timestamp"] or session["started_at"], text))
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for index, part in enumerate(parts, 1):
            title = session["title"] or None
            if title and len(parts) > 1:
                title = f"{title} (part {index}/{len(parts)})"
            docs.append(
                RawDoc(
                    external_id=f"{session['id']}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(part[-1][1], UTC),
                    text="\n".join(b[2] for b in part),
                    title_hint=title,
                    project_hint=self.default_project,
                    original_ref=f"hermes-session:{session['id']}",
                    meta={"channel": session["source"], "part": index, "parts": len(parts)},
                )
            )
        return docs

    def _render(self, row: sqlite3.Row) -> str:
        stamp = datetime.fromtimestamp(row["timestamp"], UTC).strftime("%Y-%m-%d %H:%M")
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
