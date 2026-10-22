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
from typing import TYPE_CHECKING, Any

from ..projects import DEFAULT_RULES, ProjectRules
from ..store import ConnectorState
from .base import (
    READ_ONLY_COMMAND,
    Batch,
    BuildContext,
    ConnectorType,
    PromptCleaner,
    RawDoc,
    require_dir,
    require_file,
    segment,
    whole_session,
)

if TYPE_CHECKING:
    from ..config import ConnectorConfig

_READ_ONLY_TOOLS = {"read", "grep", "glob", "list", "todoread", "webfetch", "invalid"}


class OpenCodeConnector:
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
        default_db = "~/.local/share/opencode/opencode.db"
        self.db_path = Path(os.path.expanduser(str(options.get("db_path", default_db))))
        export_dir = options.get("export_dir")
        self.export_dir = Path(os.path.expanduser(str(export_dir))) if export_dir else None
        """Session exports (`{"session": ..., "messages": [{"info", "parts"}]}`) written by
        the Jezero OpenCode plugin on another computer. When set, the database is not read."""
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.keep_read_only = bool(options.get("keep_read_only_commands", False))
        self.skip_tools = set(options.get("skip_tools", ["todowrite"]))
        self.skip_dir_globs = list(options.get("skip_dir_globs", []))
        self.include_subagents = bool(options.get("include_subagents", False))
        self.min_chars = int(options.get("min_chars", 400))
        self.project_aliases = dict(options.get("project_aliases", {}))
        self.rules = (project_rules or DEFAULT_RULES).with_aliases(self.project_aliases)
        self.prompt_cleaner = PromptCleaner(options)
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    def _connect(self) -> sqlite3.Connection:
        require_file("db_path", self.db_path)
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
        if self.export_dir is not None:
            return self._collect_exports(state, limit=limit, sample=sample, now=now)
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

    def _collect_exports(
        self, state: ConnectorState, *, limit: int | None, sample: int | None, now: float
    ) -> Batch:
        assert self.export_dir is not None
        require_dir("export_dir", self.export_dir)
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
        docs: list[RawDoc] = []
        processed: list[float] = []
        taken = 0
        for mtime, path in ready:
            if (sample is not None and taken >= sample) or (limit is not None and taken >= limit):
                break
            try:
                export = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                processed.append(mtime)
                continue
            session = export.get("session") or {}
            roles: dict[str, tuple[str, dict[str, Any]]] = {}
            parts: list[tuple[str, float, dict[str, Any]]] = []
            for message in export.get("messages") or []:
                info = message.get("info") or {}
                msg_id = str(info.get("id") or "")
                roles[msg_id] = (str(info.get("role") or "?"), info)
                msg_time = float((info.get("time") or {}).get("created") or 0)
                for part in message.get("parts") or []:
                    stamp = (part.get("time") or {}).get("start") or msg_time
                    parts.append((msg_id, float(stamp), part))
            parts.sort(key=lambda p: p[1])
            session_docs, counted = self._build_docs(
                str(session.get("id") or path.stem),
                session.get("parentID"),
                session.get("directory"),
                session.get("title"),
                roles,
                parts,
                session_file=path,
            )
            processed.append(mtime)
            docs.extend(session_docs)
            taken += 1 if (counted or sample is None) else 0
        next_cursor = repr(max(processed)) if sample is None and processed else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

    def _session_docs(self, db: sqlite3.Connection, row: tuple) -> tuple[list[RawDoc], bool]:
        session_id, parent_id, directory, title, _, _ = row
        roles: dict[str, tuple[str, dict[str, Any]]] = {}
        for msg_id, data in db.execute(
            "select id, data from message where session_id = ?", (session_id,)
        ):
            info = json.loads(data)
            roles[msg_id] = (str(info.get("role") or "?"), info)
        parts = [
            (msg_id, float(created), json.loads(data))
            for _part_id, msg_id, created, data in db.execute(
                "select id, message_id, time_created, data from part where session_id = ?"
                " order by time_created, id",
                (session_id,),
            )
        ]
        return self._build_docs(session_id, parent_id, directory, title, roles, parts)

    def _build_docs(
        self,
        session_id: str,
        parent_id: str | None,
        directory: str | None,
        title: str | None,
        roles: dict[str, tuple[str, dict[str, Any]]],
        parts: list[tuple[str, float, dict[str, Any]]],
        session_file: Path | None = None,
    ) -> tuple[list[RawDoc], bool]:
        """Shared by the database and the plugin exports. Times are epoch milliseconds."""
        if parent_id and not self.include_subagents:
            return [], False
        if any(fnmatch.fnmatch(directory or "", g) for g in self.skip_dir_globs):
            return [], False
        blocks: list[tuple[int, float, str]] = []
        for msg_id, created, part in parts:
            role = roles.get(msg_id, ("?", {}))[0]
            text = self._render(role, part, created / 1000)
            if text:
                blocks.append((len(blocks), created / 1000, text))
        if sum(len(b[2]) for b in blocks) < self.min_chars:
            return [], False
        project = (
            self.rules.project_for(directory, session_file=session_file) or self.default_project
        )
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
        return whole_session(session_id, docs), True

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


def _build(conn: ConnectorConfig, ctx: BuildContext) -> OpenCodeConnector:
    return OpenCodeConnector(
        conn.name,
        conn.options,
        max_segment_chars=ctx.max_segment_chars,
        default_project=conn.project,
        project_rules=ctx.project_rules,
    )


CONNECTOR_TYPE = ConnectorType(
    "opencode",
    "available",
    "OpenCode sessions from its SQLite store (read-only).",
    build=_build,
)
