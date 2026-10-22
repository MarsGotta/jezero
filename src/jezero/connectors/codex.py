"""OpenAI Codex CLI sessions, read from its rollout files (read-only).

Codex writes one JSON Lines "rollout" per session under `sessions/YYYY/MM/DD/`. The first
record (`session_meta`) says where the session ran and who started it. Each main session
becomes one or more docs with the dialogue: what the user typed, the agent's messages and
one short line per tool call. Dropped: injected context (AGENTS.md, environment, plugin
lists, developer instructions), reasoning, tool output and token accounting. Sessions
started by Codex itself as sub-agents (for example the approvals reviewer, which re-reads
whole transcripts) are skipped unless `include_subagents` is set.
"""

from __future__ import annotations

import functools
import glob
import json
import os
import re
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from ..projects import DEFAULT_RULES, ProjectRules
from ..store import ConnectorState
from .base import (
    READ_ONLY_COMMAND,
    Batch,
    BuildContext,
    ConnectorType,
    PromptCleaner,
    RawDoc,
    batch_full,
    glob_base,
    read_or_skip,
    require_dir,
    segment,
    whole_session,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..config import ConnectorConfig

# Text blocks that Codex injects into "user" messages.
_INJECTED_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "<user_instructions>",
    "<skills_instructions>",
    "<permissions instructions>",
    "<turn_aborted>",
    "# AGENTS.md instructions",
)
_USER_KIND = "user.text"

# Codex "code mode" wraps tool use in small scripts: tools.exec_command({cmd: "..."}),
# tools.write_stdin(...) to poll a running command, tools.apply_patch("*** Begin Patch...").
_CMD = re.compile(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')
_PATCH_FILE = re.compile(r"\*\*\* (Add|Update|Delete) File: ([^\n\\\"]+)")
_READ_ONLY = READ_ONLY_COMMAND


class CodexConnector:
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
        # `sessions_dir` may be a glob: orchestrators give each agent its own CODEX_HOME,
        # so one connector can follow every `<home>/sessions` and pick up new agents.
        self.root_pattern = os.path.expanduser(str(options["sessions_dir"]))
        self.is_glob = glob.has_magic(self.root_pattern)
        self.root = Path(self.root_pattern)
        index = options.get("session_index")
        self.session_index = Path(os.path.expanduser(str(index))) if index else None
        self.settle_seconds = float(options.get("settle_minutes", 30)) * 60
        self.include_subagents = bool(options.get("include_subagents", False))
        self.include_tool_calls = bool(options.get("include_tool_calls", True))
        self.max_tool_args = int(options.get("max_tool_args_chars", 200))
        self.keep_read_only_commands = bool(options.get("keep_read_only_commands", False))
        self.skip_tools = set(options.get("skip_tools", ["wait", "sleep", "list_agents"]))
        self.skip_cwd_globs = list(options.get("skip_cwd_globs", []))
        self.min_chars = int(options.get("min_chars", 400))
        self.project_aliases = dict(options.get("project_aliases", {}))
        self.rules = (project_rules or DEFAULT_RULES).with_aliases(self.project_aliases)
        self.prompt_cleaner = PromptCleaner(options)
        self.max_segment_chars = max_segment_chars
        self.default_project = default_project

    def collect(
        self,
        state: ConnectorState,
        *,
        limit: int | None = None,
        sample: int | None = None,
        now: float | None = None,
    ) -> Batch:
        roots = self._roots()
        if self.is_glob:
            # A glob may match no agent yet, but the folder it starts from must exist.
            require_dir("sessions_dir", _glob_base(self.root_pattern))
        else:
            require_dir("sessions_dir", self.root)
        now = time.time() if now is None else now
        settled_before = now - self.settle_seconds
        cursor = float(state.cursor) if state.cursor and sample is None else 0.0
        candidates = []
        for root in roots:
            for path in (*root.rglob("*.jsonl"), *root.rglob("*.jsonl.zst")):
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
        titles = self._titles(roots)
        docs: list[RawDoc] = []
        taken = 0
        processed: list[float] = []
        skipped: list[str] = []
        for mtime, path in ready:
            last = processed[-1] if processed else None
            if batch_full(taken, mtime, last, limit=limit, sample=sample):
                break
            read = functools.partial(self._session_docs, path, titles)
            session_docs, counted = read_or_skip(str(path), skipped, read) or ([], False)
            processed.append(mtime)
            docs.extend(session_docs)
            taken += 1 if (counted or sample is None) else 0
        next_cursor = repr(max(processed)) if sample is None and processed else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred, skipped=skipped)

    def _roots(self) -> list[Path]:
        if self.is_glob:
            return sorted(Path(p) for p in glob.glob(self.root_pattern) if Path(p).is_dir())
        return [self.root] if self.root.is_dir() else []

    def _titles(self, roots: list[Path]) -> dict[str, str]:
        titles: dict[str, str] = {}
        indexes = (
            [self.session_index]
            if self.session_index
            else [r.parent / "session_index.jsonl" for r in roots]
        )
        for index in indexes:
            if not index.is_file():
                continue
            for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("id") and row.get("thread_name"):
                    titles[str(row["id"])] = str(row["thread_name"])
        return titles

    def _session_docs(self, path: Path, titles: dict[str, str]) -> tuple[list[RawDoc], bool]:
        """Return (docs, counted). `counted` is False for sessions skipped by policy."""
        meta: dict[str, Any] | None = None
        blocks: list[tuple[int, float, str]] = []
        try:
            handle = open_text(path)
        except FileNotFoundError:
            return [], False
        with handle:
            for index, line in enumerate(handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "session_meta":
                    if meta is None:
                        meta = payload
                        if not self._wanted(meta):
                            return [], False
                    continue
                if record.get("type") != "response_item":
                    continue
                stamp = _timestamp(record.get("timestamp"))
                if stamp is None:
                    continue
                text = self._render(payload, stamp)
                if text:
                    blocks.append((index, stamp, text))
        if meta is None or not blocks or sum(len(b[2]) for b in blocks) < self.min_chars:
            return [], False
        session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
        project = self.rules.project_for(meta.get("cwd"), session_file=path) or self.default_project
        title = titles.get(session_id)
        parts = segment(blocks, self.max_segment_chars)
        docs = []
        for number, part in enumerate(parts, 1):
            part_title = (
                f"{title} (part {number}/{len(parts)})" if title and len(parts) > 1 else title
            )
            docs.append(
                RawDoc(
                    external_id=f"{session_id}#{part[0][0]}",
                    occurred_at=datetime.fromtimestamp(part[-1][1], UTC),
                    text="\n".join(b[2] for b in part),
                    title_hint=part_title,
                    project_hint=project,
                    original_ref=f"codex-session:{session_id}",
                    meta={
                        "originator": meta.get("originator") or "unknown",
                        "part": number,
                        "parts": len(parts),
                    },
                )
            )
        return whole_session(session_id, docs), True

    def _wanted(self, meta: dict[str, Any]) -> bool:
        source = meta.get("source")
        if isinstance(source, dict) and "subagent" in source and not self.include_subagents:
            return False
        cwd = str(meta.get("cwd") or "")
        return not any(fnmatch(cwd, pattern) for pattern in self.skip_cwd_globs)

    def _render(self, payload: dict[str, Any], stamp: float) -> str:
        when = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M")
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                return ""  # developer/system instructions
            texts = _message_texts(payload)
            if role == "user" and self.prompt_cleaner:
                texts = [self.prompt_cleaner.clean(t) for t in texts]
            return "\n".join(f"[{when}] {role}: {t}" for t in texts)
        if kind in ("function_call", "custom_tool_call") and self.include_tool_calls:
            name = str(payload.get("name") or "?")
            if name in self.skip_tools:
                return ""
            args = payload.get("arguments", payload.get("input", ""))
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            line = self._describe_call(name, args)
            return f"[{when}] {line}" if line else ""
        return ""

    def _describe_call(self, name: str, args: str) -> str | None:
        """One short line per call; polling and read-only inspection are dropped."""
        patched = _PATCH_FILE.findall(args)
        commands = [_unescape(c) for c in _CMD.findall(args)]
        if patched:
            files = ", ".join(f"{op.lower()} {path.strip()}" for op, path in patched)
            return self._clip(f"patch: {files}")
        if commands:
            if not self.keep_read_only_commands:
                commands = [c for c in commands if not _READ_ONLY.match(c)]
            return self._clip("shell: " + " ; ".join(commands)) if commands else None
        if "write_stdin" in args:
            return None  # polling a running command
        return self._clip(f"tool call: {name} {' '.join(args.split())}")

    def _clip(self, text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= self.max_tool_args else text[: self.max_tool_args] + "..."


def _message_texts(payload: dict[str, Any]) -> list[str]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    passthrough = payload.get("internal_chat_message_metadata_passthrough") or {}
    kinds = passthrough.get("content_item_kinds") if isinstance(passthrough, dict) else None
    aligned = isinstance(kinds, list) and len(kinds) == len(content)
    texts = []
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        if payload.get("role") == "user":
            if aligned and kinds[i] != _USER_KIND:
                continue
            if text.startswith(_INJECTED_PREFIXES):
                continue
        texts.append(text)
    return texts


def _timestamp(value: Any) -> float | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    return None


_glob_base = glob_base


def _unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


MAX_LINE_BYTES = 16 * 1024 * 1024
"""A rollout line longer than this is skipped with a warning, never held whole."""
MAX_ROLLOUT_BYTES = 512 * 1024 * 1024
"""Reading one rollout stops with a warning after this many (decompressed) bytes."""

_READ_CHUNK = 1 << 16


class BoundedLines:
    """The lines of a rollout as text, read in chunks with bounded memory.

    A line longer than `max_line` is skipped (it comes out as an empty string, so line
    numbers stay the same) and reading stops after `max_total` bytes: a small `.zst` that
    expands to gigabytes, or one gigantic line, cannot exhaust the memory of `jezero run`.
    """

    def __init__(self, raw: IO[bytes], path: Path, *, max_line: int, max_total: int) -> None:
        self._raw = raw
        self._path = path
        self._max_line = max_line
        self._max_total = max_total
        self._lines = self._generate()

    def __enter__(self) -> BoundedLines:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._raw.close()

    def __iter__(self) -> BoundedLines:
        return self

    def __next__(self) -> str:
        return next(self._lines)

    def readline(self) -> str:
        return next(self._lines, "")

    def _warn(self, message: str, *args: object) -> None:
        import logging

        logging.getLogger(__name__).warning("%s: " + message, self._path, *args)

    def _generate(self) -> Iterator[str]:
        buffer = bytearray()
        skipping = False
        total = 0
        while True:
            chunk = self._raw.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            over = total > self._max_total
            if over:
                chunk = chunk[: len(chunk) - (total - self._max_total)]
            start = 0
            while True:
                end = chunk.find(b"\n", start)
                piece = chunk[start:] if end < 0 else chunk[start : end + 1]
                if not skipping and len(buffer) + len(piece) > self._max_line:
                    self._warn("skipped a line longer than %d bytes", self._max_line)
                    buffer.clear()
                    skipping = True
                if not skipping:
                    buffer += piece
                if end < 0:
                    break
                if skipping:
                    skipping = False
                    yield ""
                else:
                    line = buffer.decode("utf-8", errors="replace")
                    buffer.clear()
                    yield line
                start = end + 1
            if over:
                self._warn("stopped reading after %d bytes (the rest is ignored)", self._max_total)
                return  # a cut last line is dropped, not parsed
        if skipping:
            yield ""
        elif buffer:
            yield buffer.decode("utf-8", errors="replace")


def open_text(path: Path) -> BoundedLines:
    """Open a rollout as lines of text; newer Codex builds compress them (`.jsonl.zst`).

    Reads are bounded by `MAX_LINE_BYTES` and `MAX_ROLLOUT_BYTES` (see `BoundedLines`).
    """
    raw: IO[bytes] = path.open("rb")
    if path.suffix == ".zst":
        import zstandard

        raw = zstandard.ZstdDecompressor().stream_reader(raw, closefd=True)  # type: ignore[assignment]
    return BoundedLines(raw, path, max_line=MAX_LINE_BYTES, max_total=MAX_ROLLOUT_BYTES)


def _build(conn: ConnectorConfig, ctx: BuildContext) -> CodexConnector:
    return CodexConnector(
        conn.name,
        conn.options,
        max_segment_chars=ctx.max_segment_chars,
        default_project=conn.project,
        project_rules=ctx.project_rules,
    )


CONNECTOR_TYPE = ConnectorType(
    "codex",
    "available",
    "OpenAI Codex CLI session transcripts (*.jsonl under a sessions directory).",
    build=_build,
)
