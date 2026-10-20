"""OpenCode connector: sessions from OpenCode's SQLite store, read-only.

OpenCode keeps sessions, messages and message parts in `opencode.db` (WAL mode). Each
session becomes one or more docs with the dialogue: user text, assistant text and one
short line per tool call that changed something (shell commands, edits, writes). Reasoning
parts, tool output, step markers and read-only tools (read, grep, glob) are dropped.
The large `event` table is never touched.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..store import ConnectorState
from .base import READ_ONLY_COMMAND, Batch, PromptCleaner, RawDoc, project_from_cwd, segment

_READ_ONLY_TOOLS = {"read", "grep", "glob", "list", "todoread", "webfetch", "invalid"}


class OpenCodeConnector:
    def __init__(
        self,
        name: str,
        options: dict[str, Any],
        *,
        max_segment_chars: int,
        default_project: str | None = None,
    ) -> None:
        self.name = name
        default_db = "~/.local/share/opencode/opencode.db"
        self.db_path = Path(os.path.expanduser(str(options.get("db_path", default_db))))
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.keep_read_only = bool(options.get("keep_read_only_commands", False))
        self.skip_tools = set(options.get("skip_tools", ["todowrite"]))
        self.skip_dir_globs = list(options.get("skip_dir_globs", []))
        self.include_subagents = bool(options.get("include_subagents", False))
        self.min_chars = int(options.get("min_chars", 400))
        self.project_aliases = dict(options.get("project_aliases", {}))
        self.prompt_cleaner = PromptCleaner(options)
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"OpenCode database not found: {self.db_path}")
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
        now: float | None = None,
    ) -> Batch:
        now = time.time() if now is None else now
        settled_before_ms = (now - self.settle_seconds) * 1000
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        db = self._connect()
        try:
            rows = db.execute(
                "select id, parent_id, directory, title, time_created, time_updated from session"
                " where time_updated > ?",
                (cursor,),
            ).fetchall()
            deferred = sum(1 for r in rows if r[5] > settled_before_ms)
            ready = sorted(
                (r for r in rows if r[5] <= settled_before_ms),
                key=lambda r: r[5],
                reverse=sample is not None,
            )
            docs: list[RawDoc] = []
            processed: list[float] = []
            taken = 0
            for row in ready:
                if (sample is not None and taken >= sample) or (
                    limit is not None and taken >= limit
                ):
                    break
                session_docs, counted = self._session_docs(db, row)
                processed.append(float(row[5]))
                docs.extend(session_docs)
                taken += 1 if (counted or sample is None) else 0
        finally:
            db.close()
        next_cursor = repr(max(processed)) if sample is None and processed else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _session_docs(self, db: sqlite3.Connection, row: tuple) -> tuple[list[RawDoc], bool]:
        session_id, parent_id, directory, title, _, _ = row
        if parent_id and not self.include_subagents:
            return [], False
        if any(fnmatch.fnmatch(directory or "", g) for g in self.skip_dir_globs):
            return [], False
        roles: dict[str, tuple[str, dict[str, Any]]] = {}
        for msg_id, data in db.execute(
            "select id, data from message where session_id = ?", (session_id,)
        ):
            info = json.loads(data)
            roles[msg_id] = (str(info.get("role") or "?"), info)
        blocks: list[tuple[int, float, str]] = []
        for _part_id, msg_id, created, data in db.execute(
            "select id, message_id, time_created, data from part where session_id = ?"
            " order by time_created, id",
            (session_id,),
        ):
            role = roles.get(msg_id, ("?", {}))[0]
            text = self._render(role, json.loads(data), created / 1000)
            if text:
                blocks.append((len(blocks), created / 1000, text))
        if sum(len(b[2]) for b in blocks) < self.min_chars:
            return [], False
        project = project_from_cwd(directory, self.project_aliases) or self.default_project
        agents = sorted({info.get("agent") for _, info in roles.values() if info.get("agent")})
        models = sorted({info.get("modelID") for _, info in roles.values() if info.get("modelID")})
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for number, part in enumerate(parts, 1):
            label = title or None
            if label and len(parts) > 1:
                label = f"{label} (part {number}/{len(parts)})"
            docs.append(
                RawDoc(
                    external_id=f"{session_id}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(part[-1][1], UTC),
                    text="\n".join(b[2] for b in part),
                    title_hint=label,
                    project_hint=project,
                    original_ref=f"opencode-session:{session_id}",
                    meta={"agents": agents, "models": models, "part": number, "parts": len(parts)},
                )
            )
        return docs, True

    def _render(self, role: str, part: dict[str, Any], stamp: float) -> str:
        when = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M")
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "").strip()
            if role == "user" and self.prompt_cleaner:
                text = self.prompt_cleaner.clean(text).strip()
            return f"[{when}] {role}: {text}" if text else ""
        if kind == "tool" and self.include_tool_calls:
            line = self._describe_tool(str(part.get("tool") or "?"), part.get("state") or {})
            return f"[{when}] {line}" if line else ""
        return ""  # reasoning, step markers, patches, snapshots

    def _describe_tool(self, tool: str, state: dict[str, Any]) -> str | None:
        if tool in self.skip_tools:
            return None
        if tool in _READ_ONLY_TOOLS and not self.keep_read_only:
            return None
        args = state.get("input") or {}
        failed = " (failed)" if state.get("status") == "error" else ""
        if tool == "bash":
            command = str(args.get("command") or "")
            if not self.keep_read_only and READ_ONLY_COMMAND.match(command):
                return None
            return self._clip(f"shell{failed}: {command}")
        if tool in ("edit", "write", "patch", "multiedit"):
            return self._clip(f"{tool}{failed}: {args.get('filePath') or args.get('path') or ''}")
        return self._clip(f"tool call{failed}: {tool} {json.dumps(args, ensure_ascii=False)}")

    def _clip(self, text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= self.max_tool_args else text[: self.max_tool_args] + "..."
