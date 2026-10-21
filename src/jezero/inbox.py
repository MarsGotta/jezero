"""The inbox: devices deliver raw session files here.

A device that runs the Jezero plugins (Claude Code, Codex, OpenCode) uploads each session
transcript when the session ends. The server only stores the file; the usual connectors
read the inbox later, exactly as they read the local stores, so summarising, redaction
and dedup stay in one place.

Layout: `<inbox>/<device>/<agent>/<relative path>`, where the relative path mirrors the
agent's own store (`projects/<slug>/<id>.jsonl` for Claude Code, `sessions/YYYY/MM/DD/
rollout-*.jsonl[.zst]` for Codex, `sessions/<id>.json` for OpenCode exports). That keeps
the existing connectors working unchanged on a device's folder.

The file's mtime is set to the upload time, not the source time: connectors keep a cursor
on mtime, and a device that was offline for a week must not upload "into the past".
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

AGENTS: dict[str, re.Pattern[str]] = {
    "claude-code": re.compile(r"projects/[A-Za-z0-9._-]{1,200}/[A-Za-z0-9._-]{1,200}\.jsonl"),
    "codex": re.compile(
        r"sessions/\d{4}/\d{2}/\d{2}/rollout-[A-Za-z0-9._:-]{1,200}\.jsonl(\.zst)?"
    ),
    "opencode": re.compile(r"sessions/[A-Za-z0-9._-]{1,200}\.json"),
}


class InboxError(ValueError):
    """The upload is refused; the message is safe to return to the device."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Stored:
    path: Path
    bytes: int


def target_path(inbox_dir: Path, device: str, agent: str, relpath: str) -> Path:
    """Validate and resolve where an upload goes. Raises InboxError."""
    if agent not in AGENTS:
        raise InboxError(f"unknown agent {agent!r}; known: {sorted(AGENTS)}")
    if not AGENTS[agent].fullmatch(relpath) or ".." in relpath.split("/"):
        raise InboxError(f"path not accepted for {agent}: {relpath!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", device):
        raise InboxError("bad device name", status=403)
    base = (inbox_dir / device / agent).resolve()
    target = (base / relpath).resolve()
    if base not in target.parents:
        raise InboxError("path escapes the inbox")
    return target


def read_body(raw: bytes, *, encoding: str | None, max_bytes: int) -> bytes:
    """Decode the request body (identity or gzip) within the size limit."""
    if encoding in (None, "", "identity"):
        if len(raw) > max_bytes:
            raise InboxError("file too large", status=413)
        return raw
    if encoding != "gzip":
        raise InboxError(f"unsupported Content-Encoding {encoding!r}", status=415)
    out = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            while chunk := stream.read(1 << 20):
                out.write(chunk)
                if out.tell() > max_bytes:
                    raise InboxError("file too large", status=413)
    except (OSError, EOFError) as exc:
        raise InboxError("body is not valid gzip") from exc
    return out.getvalue()


def store(inbox_dir: Path, device: str, agent: str, relpath: str, data: bytes) -> Stored:
    """Write atomically (temp file + rename); a newer upload of a session replaces it."""
    target = target_path(inbox_dir, device, agent, relpath)
    if not data:
        raise InboxError("empty body")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Stored(path=target, bytes=len(data))
