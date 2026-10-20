"""Antigravity CLI connector: conversations from Antigravity's local store, read-only.

Antigravity (`agy`) keeps one SQLite file per conversation under
`<data_dir>/conversations/<id>.db`. Each row of `steps` holds a protobuf payload with no
published schema, so this connector reads the wire format directly and only trusts the few
fields observed to be stable:

- step type 14, user input: field 19 -> 2 (text);
- step type 15, agent turn: field 20 -> 1 (reply text) and 20 -> 7 (tool calls: 2 name,
  3 JSON arguments, which carry a short human `toolSummary`);
- step metadata field 1: creation time (seconds, nanos).

Tool results (type 132), thinking notes, task notifications and context checkpoints are
dropped. Anything that does not parse is skipped, never guessed.

Because the format is internal, a conversation with steps from which nothing at all can be
read is reported as a warning instead of being skipped as "too short": the watchdog then
marks the connector as failing until the reader is updated. Titles and workspaces come
from `conversation_summaries.db` when it is there.
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
from urllib.parse import unquote, urlparse

from ..store import ConnectorState
from .base import READ_ONLY_COMMAND, Batch, PromptCleaner, RawDoc, project_from_cwd, segment

USER_INPUT = 14
AGENT_TURN = 15
_READ_TOOLS = {"view_file", "read_file", "list_dir", "grep_search", "find_by_name"}
_EDIT_TOOLS = {"write_to_file", "replace_file_content", "edit_file", "write_file"}
_EDIT_PATH_KEYS = ("TargetFile", "Path", "AbsolutePath")


class ProtobufError(ValueError):
    pass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ProtobufError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def pb_fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    """Top-level (field number, value) pairs of one protobuf message."""
    out: list[tuple[int, int | bytes]] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        value: int | bytes
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = buf[i : i + size], i + size
        else:
            raise ProtobufError(f"unsupported wire type {wire}")
        if i > len(buf) or field == 0:
            raise ProtobufError("malformed message")
        out.append((field, value))
    return out


def pb_get(buf: bytes, *path: int) -> list[bytes | int]:
    """Every value at a field path; branches that do not parse are ignored."""
    current: list[bytes | int] = [buf]
    for field in path:
        found: list[bytes | int] = []
        for message in current:
            if not isinstance(message, bytes):
                continue
            try:
                found += [v for f, v in pb_fields(message) if f == field]
            except ProtobufError:
                continue
        current = found
    return current


def _text(values: list[bytes | int]) -> str:
    for value in values:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
    return ""


class AntigravityConnector:
    def __init__(
        self,
        name: str,
        options: dict[str, Any],
        *,
        max_segment_chars: int,
        default_project: str | None = None,
    ) -> None:
        self.name = name
        default_dir = "~/.gemini/antigravity-cli"
        self.data_dir = Path(os.path.expanduser(str(options.get("data_dir", default_dir))))
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.keep_read_only = bool(options.get("keep_read_only_commands", False))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.skip_dir_globs = list(options.get("skip_dir_globs", []))
        self.min_chars = int(options.get("min_chars", 400))
        self.unreadable_min_steps = int(options.get("unreadable_min_steps", 2))
        self.project_aliases = dict(options.get("project_aliases", {}))
        self.prompt_cleaner = PromptCleaner(options)
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    # -- collection ---------------------------------------------------------------------

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
        now: float | None = None,
    ) -> Batch:
        conversations = self.data_dir / "conversations"
        if not conversations.is_dir():
            raise FileNotFoundError(f"Antigravity conversations not found: {conversations}")
        now = time.time() if now is None else now
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        candidates = []
        for path in conversations.glob("*.db"):
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
        info = self._summaries()
        docs: list[RawDoc] = []
        processed: list[float] = []
        unreadable: list[str] = []
        taken = 0
        for mtime, path in ready:
            if (sample is not None and taken >= sample) or (limit is not None and taken >= limit):
                break
            conversation_docs, readable = self._conversation_docs(
                path, info.get(path.stem, {}), mtime
            )
            if not readable:
                unreadable.append(path.stem)
            processed.append(mtime)
            docs.extend(conversation_docs)
            taken += 1 if (conversation_docs or sample is None) else 0
        next_cursor = repr(max(processed)) if sample is None and processed else None
        warnings = []
        if unreadable:
            warnings.append(
                f"format check: nothing readable in {len(unreadable)} conversation(s) with"
                f" steps ({', '.join(unreadable[:3])}); the Antigravity format may have changed"
            )
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred, warnings=warnings)

    def _summaries(self) -> dict[str, dict[str, Any]]:
        path = self.data_dir / "conversation_summaries.db"
        if not path.is_file():
            return {}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT conversation_id, title, workspace_uris FROM conversation_summaries"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for conversation_id, title, uris in rows:
            workspace = None
            try:
                first = (json.loads(uris) if uris else [None])[0]
                if first and str(first).startswith("file://"):
                    workspace = unquote(urlparse(str(first)).path)
            except (json.JSONDecodeError, IndexError, TypeError):
                workspace = None
            out[str(conversation_id)] = {"title": title or None, "workspace": workspace}
        return out

    def _conversation_docs(
        self, path: Path, info: dict[str, Any], mtime: float
    ) -> tuple[list[RawDoc], bool]:
        """Return (docs, readable). `readable` is False when the conversation has steps
        but the reader recognised none of them."""
        workspace = info.get("workspace")
        if workspace and any(fnmatch.fnmatch(workspace, g) for g in self.skip_dir_globs):
            return [], True
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return [], False
        blocks: list[tuple[int, float, str]] = []
        stamp = mtime
        for _idx, step_type, metadata, payload in rows:
            stamp = self._stamp(metadata) or stamp
            if not isinstance(payload, bytes):
                continue
            for text in self._render(step_type, payload, stamp):
                blocks.append((len(blocks), stamp, text))
        readable = bool(blocks) or len(rows) < self.unreadable_min_steps
        if sum(len(b[2]) for b in blocks) < self.min_chars:
            return [], readable
        project = project_from_cwd(workspace, self.project_aliases) or self.default_project
        title = info.get("title")
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for number, part in enumerate(parts, 1):
            label = title
            if label and len(parts) > 1:
                label = f"{label} (part {number}/{len(parts)})"
            docs.append(
                RawDoc(
                    external_id=f"{path.stem}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(part[-1][1], UTC),
                    text="\n".join(b[2] for b in part),
                    title_hint=label,
                    project_hint=project,
                    original_ref=f"antigravity-conversation:{path.stem}",
                    meta={"part": number, "parts": len(parts)},
                )
            )
        return docs, True

    @staticmethod
    def _stamp(metadata: Any) -> float | None:
        if not isinstance(metadata, bytes):
            return None
        seconds = [v for v in pb_get(metadata, 1, 1) if isinstance(v, int)]
        return float(seconds[0]) if seconds and seconds[0] > 1_000_000_000 else None

    # -- rendering ----------------------------------------------------------------------

    def _render(self, step_type: int, payload: bytes, stamp: float) -> list[str]:
        when = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M")
        if step_type == USER_INPUT:
            text = _text(pb_get(payload, 19, 2))
            if text and self.prompt_cleaner:
                text = self.prompt_cleaner.clean(text).strip()
            return [f"[{when}] user: {text}"] if text else []
        if step_type != AGENT_TURN:
            return []  # tool results, notifications, checkpoints
        lines = []
        if self.include_tool_calls:
            for call in pb_get(payload, 20, 7):
                if isinstance(call, bytes):
                    line = self._describe_call(call)
                    if line:
                        lines.append(f"[{when}] {line}")
        reply = _text(pb_get(payload, 20, 1))
        if reply:
            lines.append(f"[{when}] assistant: {reply}")
        return lines

    def _describe_call(self, call: bytes) -> str | None:
        name = _text(pb_get(call, 2)) or "?"
        try:
            args = json.loads(_text(pb_get(call, 3)) or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        summary = str(args.get("toolSummary") or "").strip()
        if name in _READ_TOOLS and not self.keep_read_only:
            return None
        if name == "run_command":
            command = str(args.get("CommandLine") or "")
            if not self.keep_read_only and READ_ONLY_COMMAND.match(command):
                return None
            label = f"shell ({summary})" if summary else "shell"
            return self._clip(f"{label}: {command}")
        if name in _EDIT_TOOLS:
            target = next((args[k] for k in _EDIT_PATH_KEYS if args.get(k)), "")
            return self._clip(f"edit: {target}")
        if name == "search_web":
            return self._clip(f"web search: {args.get('query') or ''}")
        if name == "read_url_content":
            return self._clip(f"read url: {args.get('Url') or ''}")
        if summary:
            return self._clip(f"{name}: {summary}")
        return None

    def _clip(self, text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= self.max_tool_args else text[: self.max_tool_args] + "..."
