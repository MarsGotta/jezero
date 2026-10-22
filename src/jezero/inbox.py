"""The inbox: devices deliver raw session files here.

A device that runs the Jezero plugins (Claude Code, Codex, OpenCode, Hermes, Antigravity)
uploads each session transcript when the session ends. The server only stores the file; the usual
connectors read the inbox later, exactly as they read the local stores, so summarising,
redaction and dedup stay in one place.

Layout: `<inbox>/<device>/<agent>/<relative path>`, where the relative path mirrors the
agent's own store (`projects/<slug>/<id>.jsonl` for Claude Code, `sessions/YYYY/MM/DD/
rollout-*.jsonl[.zst]` for Codex, `sessions/<id>.json` for OpenCode and Hermes exports,
`brain/<id>/transcript.jsonl` for Antigravity).
That keeps the existing connectors working unchanged on a device's folder.

The file's mtime is set to the upload time, not the source time: connectors keep a cursor
on mtime, and a device that was offline for a week must not upload "into the past".
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

from .projects import SIDECAR_SUFFIX

AGENTS: dict[str, re.Pattern[str]] = {
    "claude-code": re.compile(r"projects/[A-Za-z0-9._-]{1,200}/[A-Za-z0-9._-]{1,200}\.jsonl"),
    "codex": re.compile(
        r"sessions/\d{4}/\d{2}/\d{2}/rollout-[A-Za-z0-9._:-]{1,200}\.jsonl(\.zst)?"
    ),
    "opencode": re.compile(r"sessions/[A-Za-z0-9._-]{1,200}\.json"),
    "hermes": re.compile(r"sessions/[A-Za-z0-9._-]{1,200}\.json"),
    "antigravity": re.compile(r"brain/[A-Za-z0-9-]{8,64}/transcript\.jsonl"),
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
    """Decode the request body (identity, gzip or zstd) within the size limit."""
    encoding = (encoding or "").strip().lower()
    if encoding in ("", "identity"):
        if len(raw) > max_bytes:
            raise InboxError("file too large", status=413)
        return raw
    if encoding == "gzip":
        stream: Any = gzip.GzipFile(fileobj=io.BytesIO(raw))
        errors: tuple[type[BaseException], ...] = (OSError, EOFError)
    elif encoding == "zstd":
        stream = zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(raw), read_across_frames=True
        )
        errors = (zstandard.ZstdError, OSError, EOFError)
    else:
        raise InboxError(
            f"unsupported Content-Encoding {encoding!r}; use gzip, zstd or none", status=415
        )
    out = io.BytesIO()
    try:
        with stream:
            while chunk := stream.read(1 << 20):
                out.write(chunk)
                if out.tell() > max_bytes:
                    raise InboxError("file too large", status=413)
    except errors as exc:
        raise InboxError(f"body is not valid {encoding}") from exc
    return out.getvalue()


async def read_stream(chunks: AsyncIterator[bytes], *, max_bytes: int) -> bytes:
    """Collect a request body chunk by chunk, refusing it (413) as soon as it passes
    `max_bytes`: a body sent without Content-Length never sits whole in memory."""
    buffer = bytearray()
    async for chunk in chunks:
        if len(buffer) + len(chunk) > max_bytes:
            raise InboxError("file too large", status=413)
        buffer.extend(chunk)
    return bytes(buffer)


_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def store(
    inbox_dir: Path,
    device: str,
    agent: str,
    relpath: str,
    data: bytes,
    project: str | None = None,
) -> Stored:
    """Write atomically (temp file + rename); a newer upload of a session replaces it.

    `project` is what the device's client found for the session (its git repository or
    configured folders); it goes next to the file as `<file>.project` for the connectors.
    """
    target = target_path(inbox_dir, device, agent, relpath)
    if not data:
        raise InboxError("empty body")
    if project is not None and not _PROJECT.match(project):
        raise InboxError("invalid project name")
    target.parent.mkdir(parents=True, exist_ok=True)
    sidecar = Path(str(target) + SIDECAR_SUFFIX)
    if project:
        sidecar.write_text(project + "\n", encoding="utf-8")
    else:
        sidecar.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Stored(path=target, bytes=len(data))
