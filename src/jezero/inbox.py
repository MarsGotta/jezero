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

A file whose own name says it is compressed (Codex's `.jsonl.zst`) is stored as sent, but
only after it was decompressed in chunks and found to stay within the size limit: a small
file that expands to gigabytes is refused at the door, not when a connector opens it.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

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


def check_compressed_file(relpath: str, data: bytes, *, max_bytes: int) -> None:
    """Refuse (413) a `.zst` file that decompresses beyond `max_bytes`.

    The file is decompressed in chunks and nothing is kept. Bytes that are not valid zstd
    are stored as sent, like any other file the connector will fail to read: they cannot
    expand, and the connector's own reads are bounded too.
    """
    if not relpath.endswith(".zst"):
        return
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
    total = 0
    try:
        with reader:
            while chunk := reader.read(1 << 20):
                total += len(chunk)
                if total > max_bytes:
                    raise InboxError("file too large", status=413)
    except (zstandard.ZstdError, OSError, EOFError):
        return


def device_usage(inbox_dir: Path, device: str, *, skip: Path | None = None) -> int:
    """Bytes in a device's inbox folder, without `skip` (the file an upload replaces).

    Walks the whole folder; `store` uses the cached `_USAGE` instead.
    """
    total = 0
    for root, _, files in os.walk(inbox_dir / device):
        for name in files:
            path = Path(root) / name
            if skip is not None and path == skip:
                continue
            try:
                total += path.lstat().st_size
            except OSError:
                continue
    return total


USAGE_RECOMPUTE_SECONDS = 300.0
"""A device's cached folder size is walked again at least this often (files removed by
hand or by a cleanup job are seen within this time)."""


class _UsageCache:
    """Bytes used per device folder, kept up to date by each accepted upload.

    Walking a folder of tens of thousands of files on every upload is slow; the total is
    computed once, adjusted by every write, and recomputed every
    `USAGE_RECOMPUTE_SECONDS`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[Path, tuple[int, float]] = {}

    def get(self, folder: Path) -> int:
        with self._lock:
            cached = self._totals.get(folder)
            if cached is not None and time.monotonic() - cached[1] < USAGE_RECOMPUTE_SECONDS:
                return cached[0]
        total = device_usage(folder.parent, folder.name)
        with self._lock:
            self._totals[folder] = (total, time.monotonic())
        return total

    def add(self, folder: Path, delta: int) -> None:
        with self._lock:
            cached = self._totals.get(folder)
            if cached is not None:
                self._totals[folder] = (max(cached[0] + delta, 0), cached[1])

    def clear(self) -> None:
        with self._lock:
            self._totals.clear()


_USAGE = _UsageCache()


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


async def read_stream(chunks: AsyncIterator[bytes], *, max_bytes: int) -> bytes:
    """Collect a request body chunk by chunk, refusing it (413) as soon as it passes
    `max_bytes`: a body sent without Content-Length never sits whole in memory."""
    buffer = bytearray()
    async for chunk in chunks:
        if len(buffer) + len(chunk) > max_bytes:
            raise InboxError("file too large", status=413)
        buffer.extend(chunk)
    return bytes(buffer)


_PROJECT = re.compile(r"[^\W_][\w.-]{0,63}")
"""A project name: letters of any script (`diseño`), digits, `.`, `_` and `-`, starting
with a letter or digit, at most 64 characters. Matched with fullmatch, after NFC."""


def project_from_header(value: str | None) -> str | None:
    """The `X-Jezero-Project` header as a project name (None when absent or empty).

    The value is percent-decoded as UTF-8 (`dise%C3%B1o`); raw UTF-8 bytes in the header
    are accepted too. The name is NFC-normalised, then checked by `store`.
    """
    if not value:
        return None
    if len(value) > 1024:
        raise InboxError("invalid project name")
    if not value.isascii():
        # Starlette decodes header bytes as Latin-1: get the UTF-8 the client sent.
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            raise InboxError("invalid project name") from exc
    try:
        value = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InboxError("invalid project name") from exc
    return unicodedata.normalize("NFC", value) or None


def store(
    inbox_dir: Path,
    device: str,
    agent: str,
    relpath: str,
    data: bytes,
    project: str | None = None,
    *,
    max_device_bytes: int | None = None,
) -> Stored:
    """Write atomically (temp file + rename); a newer upload of a session replaces it.

    `project` is what the device's client found for the session (its git repository or
    configured folders); it goes next to the file as `<file>.project` for the connectors.
    With `max_device_bytes`, an upload that would take the device's folder past it is
    refused with 507 (the file it replaces does not count). The folder's size is cached
    (see `_UsageCache`), not walked on every upload. Two uploads of one device at the same
    moment may each pass the check, so the folder can end one file over.
    """
    target = target_path(inbox_dir, device, agent, relpath)
    if not data:
        raise InboxError("empty body")
    if project is not None:
        project = unicodedata.normalize("NFC", project)
        if not _PROJECT.fullmatch(project):
            raise InboxError("invalid project name")
    folder = inbox_dir.resolve() / device
    sidecar = Path(str(target) + SIDECAR_SUFFIX)
    before = _size(target) + _size(sidecar)
    if max_device_bytes is not None:
        used = _USAGE.get(folder) - _size(target)
        if used + len(data) > max_device_bytes:
            raise InboxError(
                "device inbox full: the server's inbox.max_device_mb is reached", status=507
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
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
    finally:
        _USAGE.add(folder, _size(target) + _size(sidecar) - before)
    return Stored(path=target, bytes=len(data))
