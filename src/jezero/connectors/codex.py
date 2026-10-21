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
    segment,
)

if TYPE_CHECKING:
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
        if not roots and not self.is_glob:
            raise FileNotFoundError(f"Codex sessions directory not found: {self.root}")
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
        for mtime, path in ready:
            if (sample is not None and taken >= sample) or (limit is not None and taken >= limit):
                break
            session_docs, counted = self._session_docs(path, titles)
            processed.append(mtime)
            docs.extend(session_docs)
            taken += 1 if (counted or sample is None) else 0
        next_cursor = repr(max(processed)) if sample is None and processed else None
        return Batch(docs=docs, next_cursor=next_cursor, deferred=deferred)

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
        return docs, True

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


def _unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def open_text(path: Path) -> IO[str]:
    """Open a rollout as text; newer Codex builds compress them (`.jsonl.zst`)."""
    if path.suffix != ".zst":
        return path.open(encoding="utf-8", errors="replace")
    import io

    import zstandard

    raw = path.open("rb")
    stream = zstandard.ZstdDecompressor().stream_reader(raw, closefd=True)
    return io.TextIOWrapper(stream, encoding="utf-8", errors="replace")


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
