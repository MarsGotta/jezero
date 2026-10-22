#!/usr/bin/env python3
"""Jezero client: the shared core of the Jezero plugins for Claude Code, Codex, OpenCode
and Hermes Agent.

The plugins are thin. Each one only maps its agent's events to a command of this file
(the Hermes plugin imports it and calls the same functions):

    session-start   print the Jezero protocol and recent work of this project (context);
                    nothing on a computer without a key
    user-prompt     on the first prompt of a session, print past work that matches it
    stop            note the session transcript and upload it in the background (throttled)
    session-end     same, without throttling

Other commands: init (create this device's key), status, setup (register the MCP server in
the agents), mcp (stdio MCP bridge that holds the key), flush (upload what is queued),
backfill (queue existing sessions), protocol.

A computer set up with `init --no-upload` only reads Jezero: hooks give context, nothing
is queued or uploaded (for the computer that runs the server, whose sessions the
connectors already read).

Standard library only, Python 3.9+, so it runs with the python3 that ships with macOS.
Nothing here may block an agent: every network call has a short timeout, hooks never fail
loudly, and uploads run in a detached process.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

VERSION = "0.5.0"
AGENT_HOME_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
AGENT_HOME_VAR = {"claude-code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def client_home() -> Path:
    """Where this device's key lives: `JEZERO_CLIENT_HOME`; else a `jezero` folder inside
    the agent's own config folder when it holds a key (sandboxed agents, which cannot see
    the user's home, keep their own key there); else `~/.jezero`."""
    explicit = os.environ.get("JEZERO_CLIENT_HOME")
    if explicit:
        return Path(explicit).expanduser()
    for var in AGENT_HOME_VARS:
        base = os.environ.get(var)
        if base and (Path(base).expanduser() / "jezero" / "client.json").is_file():
            return Path(base).expanduser() / "jezero"
    return Path("~/.jezero").expanduser()


def use_home(home: Path) -> None:
    """Point every client file at `home` (the key, queue, state and log live together)."""
    global HOME, CONFIG, TOKEN_FILE, SPOOL, STATE, LOG, CA_FILE
    HOME = home
    CONFIG = HOME / "client.json"
    TOKEN_FILE = HOME / "token"
    SPOOL = HOME / "spool"
    STATE = HOME / "state.json"
    LOG = HOME / "client.log"
    CA_FILE = HOME / "ca.pem"


def agent_home(agent: str | None) -> Path:
    """The client home of one agent: `JEZERO_CLIENT_HOME`; else the `jezero` folder in that
    agent's own config folder when it holds a key (`$CLAUDE_CONFIG_DIR/jezero`,
    `$CODEX_HOME/jezero`, or `~/.gemini/config/jezero` for Antigravity, whose sandboxed
    agents may see that folder but not `~/.jezero`); else `client_home()`."""
    explicit = os.environ.get("JEZERO_CLIENT_HOME")
    if explicit:
        return Path(explicit).expanduser()
    own: Path | None = None
    if agent == "antigravity":
        own = Path.home() / ".gemini" / "config" / "jezero"
    elif agent in AGENT_HOME_VAR and os.environ.get(AGENT_HOME_VAR[agent]):
        own = Path(os.environ[AGENT_HOME_VAR[agent]]).expanduser() / "jezero"
    if own is not None and (own / "client.json").is_file():
        return own
    return client_home()


def use_agent_home(agent: str | None) -> None:
    """Point every client file at `agent_home(agent)`; without an agent nothing changes."""
    if agent:
        use_home(agent_home(agent))


HOME = CONFIG = TOKEN_FILE = SPOOL = STATE = LOG = CA_FILE = Path()
use_home(client_home())
AGENTS = ("claude-code", "codex", "opencode", "hermes", "antigravity")
SETUP_AGENTS = AGENTS

HOOK_TIMEOUT = float(os.environ.get("JEZERO_HOOK_TIMEOUT", "2.5"))
STOP_THROTTLE_SECONDS = 15 * 60
RECENT_ITEMS = 5
PROMPT_HITS = 3
DEFAULT_MCP_PATH = "/mcp"
STATE_LIMITS = {"uploaded": 2000, "rejected": 500, "stop_flush": 500, "prompted": 500}
"""Entries kept in state.json per table (the newest ones): it must not grow forever."""

PROTOCOL = """\
## Jezero: shared memory of past work (read-only)

Jezero indexes past sessions of all of this user's agents and their project tasks: \
decisions, fixes, exact errors, commands and facts, each with the date it happened. \
Use it out of habit, without waiting to be asked:

- Before non-trivial work in a project, or when the user mentions something you have \
no context on, call the jezero `search` tool with 2-5 distinctive words (names, errors, \
ids). Matching is literal: try a synonym if nothing comes back.
- Before relying on a decision, plan, status or number from Jezero, call `later` on that \
item: a later session may have changed it. Answer with the newest state.
- Cite item ids like [123] when an answer comes from Jezero. If Jezero has nothing, say \
so; never fill the gap with a guess.
- Jezero is read-only and ingests sessions by itself: there is nothing to save."""


# --------------------------------------------------------------------------- config


def log(message: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    with contextlib.suppress(OSError):
        os.chmod(str(path), 0o600)


def load_config() -> dict[str, Any] | None:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        cfg["token"] = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return cfg if cfg.get("url") and cfg["token"] else None
    except (OSError, ValueError):
        return None


def uploads_enabled(cfg: dict[str, Any] | None) -> bool:
    return bool(cfg) and cfg.get("upload", True) is not False


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _stamp(value: Any) -> float:
    """When a state entry was written: a time, an upload mark [mtime, size] or a record."""
    if isinstance(value, dict):
        value = value.get("at")
    elif isinstance(value, list):
        value = value[0] if value else 0
    return float(value) if isinstance(value, (int, float)) else 0.0


def _prune(state: dict[str, Any]) -> None:
    for name, keep in STATE_LIMITS.items():
        table = state.get(name)
        if isinstance(table, dict) and len(table) > keep:
            for key in sorted(table, key=lambda k: _stamp(table[k]))[:-keep]:
                table.pop(key, None)


def load_state() -> dict[str, Any]:
    state = _load_json(STATE, {})
    return state if isinstance(state, dict) else {}


def update_state(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read state.json again, apply `change`, prune and save (hooks and the uploader write
    it at the same time, so never save a copy read long ago)."""
    state = load_state()
    change(state)
    _prune(state)
    _save_json(STATE, state)
    return state


def _table(state: dict[str, Any], name: str) -> dict[str, Any]:
    table = state.get(name)
    if not isinstance(table, dict):
        table = state[name] = {}
    return table


# ----------------------------------------------------------------------------- http


def _http(
    method: str,
    url: str,
    token: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    response_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", f"jezero-client/{VERSION}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                if response_headers is not None:
                    response_headers.update(response.headers.items())
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            if attempt == 1 and _is_cert_error(exc) and export_system_roots():
                continue
            raise
    raise AssertionError("unreachable")


def _ssl_context() -> ssl.SSLContext:
    """Default trust, or the system roots exported by `export_system_roots`."""
    cafile = os.environ.get("JEZERO_CA_FILE") or (str(CA_FILE) if CA_FILE.is_file() else None)
    return ssl.create_default_context(cafile=cafile)


def _is_cert_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(
        exc
    )


def export_system_roots() -> bool:
    """macOS: Python may not trust the system's certificates (python.org builds ship none,
    and company networks add their own root to the keychain). Export the keychains' roots
    to `ca.pem` in the client home (`~/.jezero` unless the key lives elsewhere, see
    `client_home`) once and use them from then on. False if nothing changed."""
    if sys.platform != "darwin" or CA_FILE.is_file() or os.environ.get("JEZERO_CA_FILE"):
        return False
    keychains = [
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/Library/Keychains/System.keychain",
        str(Path("~/Library/Keychains/login.keychain-db").expanduser()),
    ]
    pems = []
    for keychain in keychains:
        if not Path(keychain).exists():
            continue
        result = subprocess.run(
            ["/usr/bin/security", "find-certificate", "-a", "-p", keychain],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            pems.append(result.stdout)
    bundle = "".join(pems)
    if "BEGIN CERTIFICATE" not in bundle:
        return False
    _write_private(CA_FILE, bundle)
    log(f"exported system certificates to {CA_FILE}")
    return True


def mcp_path(value: Any) -> str:
    """`mcp_path` as a URL path: one leading slash, no trailing one (default /mcp)."""
    text = str(value or "").strip().strip("/")
    return "/" + text if text else DEFAULT_MCP_PATH


def mcp_url(cfg: dict[str, Any]) -> str:
    """The server's MCP endpoint: `url` plus `mcp_path` from client.json (the server's
    `[server].path`, /mcp unless its admin changed it)."""
    return str(cfg["url"]).rstrip("/") + mcp_path(cfg.get("mcp_path"))


def mcp_call(cfg: dict[str, Any], tool: str, args: dict[str, Any], timeout: float) -> Any:
    """Call one tool of the Jezero MCP server (stateless JSON-RPC over HTTP)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    status, raw = _http(
        "POST",
        mcp_url(cfg),
        cfg["token"],
        body=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"mcp {tool}: HTTP {status}")
    text = raw.decode("utf-8", "replace")
    if text.lstrip().startswith("event:") or text.lstrip().startswith("data:"):
        text = [line[5:] for line in text.splitlines() if line.startswith("data:")][-1]
    result = json.loads(text).get("result") or {}
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for block in result.get("content") or []:
        if block.get("type") == "text":
            return json.loads(block["text"])
    return None


# -------------------------------------------------------------------------- context

try:  # shipped next to this file in every plugin
    from jezero_projects import ProjectRules
except ImportError:  # running from the repository
    from jezero.projects import ProjectRules


def project_rules() -> ProjectRules:
    """`projects` in client.json: paths, roots, aliases, git (all optional)."""
    data = _load_json(CONFIG, {})
    return ProjectRules.from_dict(data.get("projects") if isinstance(data, dict) else None)


def project_from_cwd(cwd: str | None) -> str | None:
    return project_rules().project_for(cwd)


def _line(item: dict[str, Any]) -> str:
    date = str(item.get("occurred_at") or "")[:10]
    title = " ".join(str(item.get("title") or "").split())
    return f"- [{item.get('id')}] {date} {title}"


def recent_context(cfg: dict[str, Any], cwd: str | None) -> str:
    project = project_from_cwd(cwd)
    if not project:
        return ""
    data = mcp_call(cfg, "timeline", {"project": project, "limit": RECENT_ITEMS}, HOOK_TIMEOUT)
    items = (data or {}).get("items") or []
    if not items:
        return ""
    lines = [f"Recent work on project `{project}` in Jezero (newest first):"]
    lines += [_line(i) for i in items]
    return "\n".join(lines)


_STOP = set(
    """a al algo ante antes aqui asi aun bien cada como con contra cual cuando de del desde
    donde dos el ella ellos en entre era es esa ese eso esta este esto estos fue ha hay hace
    hacer hoy la las le les lo los mas me mi mis muy nada ni no nos o otra otro para pero
    poco por porque puede que quien quiero se sea ser si sin sobre solo son su sus tal
    tambien tan te tengo tiene todo tu un una uno unos ya yo hola gracias favor ayudame
    ayuda revisa arregla haz dime puedes podrias necesito vale bueno sigue sigamos vamos
    the an and or but if then this that these those is are was were be been to of in on at
    for with from by as it its you your we our can could would should please help me my do
    does did make let want need just about into than more most some any all fix now also
    """.split()
)


def _norm(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def prompt_terms(prompt: str) -> list[str]:
    """Distinctive words of a prompt: no stop words, at least 3 characters, unique."""
    seen: list[str] = []
    for word in re.findall(r"[\w.-]{3,}", _norm(prompt)):
        word = word.strip(".-")
        if len(word) >= 3 and word not in _STOP and word not in seen:
            seen.append(word)
    return seen[:20]


def related(hit: dict[str, Any], terms: list[str]) -> bool:
    """BM25 scores are not comparable across queries, so require evidence instead: at
    least two of the prompt's words appear as whole words in the hit's title or summary."""
    text = _norm(" ".join(str(hit.get(k) or "") for k in ("title", "summary")))
    found = sum(1 for t in terms if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", text))
    return found >= 2


def prompt_context(cfg: dict[str, Any], prompt: str, cwd: str | None) -> str:
    terms = prompt_terms(prompt)
    if len(terms) < 2:
        return ""
    data = mcp_call(cfg, "search", {"query": " ".join(terms), "limit": 8}, HOOK_TIMEOUT)
    hits = [h for h in (data or {}).get("results") or [] if related(h, terms)][:PROMPT_HITS]
    if not hits:
        return ""
    lines = ["Jezero has past work that may be related (check with `get_item` before using):"]
    lines += [_line(h) for h in hits]
    return "\n".join(lines)


# -------------------------------------------------------------------- spool / upload


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:150]


def relpath_for(agent: str, path: Path) -> str | None:
    """Where a transcript goes in the server inbox (mirrors the agent's own store)."""
    parts = path.parts
    if agent == "claude-code":
        if "subagents" in parts or path.suffix != ".jsonl" or len(parts) < 2:
            return None
        return f"projects/{_safe(parts[-2])}/{_safe(parts[-1])}"
    if agent == "codex":
        name = path.name
        if not name.startswith("rollout-") or not (
            name.endswith(".jsonl") or name.endswith(".jsonl.zst")
        ):
            return None
        if "sessions" in parts:
            tail = parts[parts.index("sessions") + 1 :]
            if len(tail) == 4 and all(p.isdigit() for p in tail[:3]):
                return "sessions/" + "/".join(tail)
        match = re.search(r"rollout-(\d{4})-(\d{2})-(\d{2})", name)
        if match:
            return f"sessions/{match.group(1)}/{match.group(2)}/{match.group(3)}/{_safe(name)}"
        return None
    if agent in ("opencode", "hermes"):
        return f"sessions/{_safe(path.stem)}.json" if path.suffix == ".json" else None
    if agent == "antigravity":
        # <data dir>/brain/<conversation>/.system_generated/logs/transcript.jsonl
        if path.name != "transcript.jsonl" or "brain" not in parts:
            return None
        rest = parts[parts.index("brain") + 1 :]
        return f"brain/{_safe(rest[0])}/transcript.jsonl" if len(rest) > 1 else None
    return None


def cwd_of(path: Path) -> str | None:
    """The working folder recorded in a transcript (first lines of Claude Code or Codex
    JSONL, or the session record of a Hermes export)."""
    if path.suffix == ".json" and path.parent.name == "hermes":
        session = (_load_json(path, {}) or {}).get("session") or {}
        found = session.get("cwd") if isinstance(session, dict) else None
        return found if isinstance(found, str) and found else None
    if path.suffix != ".jsonl":
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(50), handle):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                found = record.get("cwd") or (
                    payload.get("cwd") if isinstance(payload, dict) else None
                )
                if isinstance(found, str) and found:
                    return found
    except OSError:
        return None
    return None


HERMES_SKIP_SOURCES = ("cron", "subagent")
"""Hermes sessions that are not conversations worth keeping (scheduled jobs, subagents)."""


def hermes_state_db(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(home).expanduser() / "state.db"


def export_hermes(state_db: Path, session_id: str) -> Path | None:
    """Write one Hermes session (dialogue and tool calls) from its state.db to the spool as
    JSON, the shape the server's hermes connector reads. The file is rewritten only when
    the session changed, so an unchanged session is not uploaded again."""
    import sqlite3

    if not state_db.is_file() or not session_id:
        return None
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        session_cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        wanted = [c for c in ("id", "source", "title", "started_at", "cwd") if c in session_cols]
        row = con.execute(
            f"SELECT {', '.join(wanted)} FROM sessions WHERE id = ?",  # noqa: S608
            (session_id,),
        ).fetchone()
        if row is None or ("source" in wanted and row["source"] in HERMES_SKIP_SOURCES):
            return None
        message_cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        active = " AND active = 1" if "active" in message_cols else ""
        messages = con.execute(
            "SELECT id, role, content, tool_calls, timestamp FROM messages "  # noqa: S608
            f"WHERE session_id = ? AND role IN ('user', 'assistant'){active} ORDER BY id",
            (session_id,),
        ).fetchall()
    finally:
        con.close()
    if not messages:
        return None
    data = {
        "format": "jezero-hermes-export/1",
        "session": {k: row[k] for k in wanted},
        "messages": [dict(m) for m in messages],
    }
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    target = SPOOL / "hermes" / f"{_safe(session_id)}.json"
    try:
        if target.read_text(encoding="utf-8") == text:
            return target
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(target))
    return target


def enqueue(agent: str, session_id: str, transcript: str, cwd: str | None) -> bool:
    path = Path(transcript).expanduser()
    if relpath_for(agent, path) is None:
        return False
    cwd = cwd or cwd_of(path)
    pointer = {
        "agent": agent,
        "session_id": session_id,
        "path": str(path),
        "cwd": cwd,
        "project": project_from_cwd(cwd),
        "queued_at": time.time(),
    }
    name = f"{agent}__{_safe(session_id or path.stem)}.json"
    _save_json(SPOOL / "pending" / name, pointer)
    return True


def spawn_flush() -> None:
    """Upload in a detached process so the agent never waits for the network. The child
    gets this home explicitly: a key kept in an agent's own folder (Antigravity, sandboxes)
    is not where a plain `flush` would look."""
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": {**os.environ, "JEZERO_CLIENT_HOME": str(HOME)},
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "flush"], **kwargs)
    except OSError as exc:
        log(f"spawn flush failed: {exc}")


class _Lock:
    """Single uploader at a time (a lock file that goes stale after 10 minutes)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def __enter__(self) -> _Lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > 600:
                self.path.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except FileExistsError:
            self.held = False
        return self

    def __exit__(self, *_: Any) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                self.path.unlink()


class KeyRefused(RuntimeError):
    """HTTP 401/403: the key is not approved yet, or not allowed to upload. The queue stays:
    once the admin approves the key for uploads, the files go out."""


class Rejected(RuntimeError):
    """HTTP 400/413/415: the server will never accept this file as it is."""


def upload(cfg: dict[str, Any], agent: str, path: Path, project: str | None = None) -> bool:
    """Send one file. True when sent (or nothing to send); raises `KeyRefused`, `Rejected`,
    or another error when the server is down."""
    relpath = relpath_for(agent, path)
    if relpath is None:
        return True  # nothing to send, not an error
    data = path.read_bytes()
    url = cfg["url"].rstrip("/") + f"/inbox/v1/{agent}/{relpath}"
    status, raw = _http(
        "PUT",
        url,
        cfg["token"],
        body=gzip.compress(data),
        headers={
            "Content-Encoding": "gzip",
            "Content-Type": "application/octet-stream",
            **({"X-Jezero-Project": project} if project else {}),
        },
        timeout=60,
    )
    if status == 200:
        return True
    detail = " ".join(raw[:200].decode("utf-8", "replace").split())
    reason = f"HTTP {status} {detail}".strip()
    if status in (401, 403):
        raise KeyRefused(reason)
    if status in (400, 413, 415):
        raise Rejected(reason)
    raise RuntimeError(reason)


def flush(cfg: dict[str, Any]) -> dict[str, int]:
    """Upload the queue in order. Offline, a server error or a refused key stop the run and
    keep the queue; a file the server rejects is dropped and recorded as rejected."""
    counts = {"sent": 0, "unchanged": 0, "gone": 0, "failed": 0, "rejected": 0}
    with _Lock(SPOOL / "flush.lock") as lock:
        if not lock.held:
            return counts
        state = load_state()
        uploaded = _table(state, "uploaded")
        rejected = _table(state, "rejected")
        refused: str | None = None
        for pointer_file in sorted((SPOOL / "pending").glob("*.json")):
            pointer = _load_json(pointer_file, None)
            if not isinstance(pointer, dict) or not pointer.get("path"):
                with contextlib.suppress(OSError):
                    pointer_file.unlink()
                continue
            path = Path(pointer["path"])
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                counts["gone"] += 1
                pointer_file.unlink()
                continue
            mark = [stat.st_mtime, float(stat.st_size)]
            old_reject = rejected.get(key)
            if uploaded.get(key) == mark or (
                isinstance(old_reject, dict) and old_reject.get("mark") == mark
            ):
                counts["unchanged"] += 1
                pointer_file.unlink()
                continue
            try:
                upload(cfg, pointer["agent"], path, pointer.get("project"))
            except KeyRefused as exc:
                counts["failed"] += 1
                refused = str(exc)
                break  # the key may be approved for uploads later: keep the whole queue
            except Rejected as exc:
                counts["rejected"] += 1
                entry = {
                    "agent": pointer.get("agent"),
                    "mark": mark,
                    "reason": str(exc),
                    "at": time.time(),
                }
                rejected[key] = entry
                log(f"upload of {path.name} rejected ({exc}); not retried until it changes")
                pointer_file.unlink()
                update_state(lambda s, k=key, e=entry: _table(s, "rejected").__setitem__(k, e))
                continue
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                counts["failed"] += 1
                log(f"flush stopped at {path.name}: {exc}")
                break  # offline or server down: keep the queue for next time
            uploaded[key] = mark
            rejected.pop(key, None)
            counts["sent"] += 1
            pointer_file.unlink()

            def sent(s: dict[str, Any], k: str = key, m: list[float] = mark) -> None:
                _table(s, "uploaded")[k] = m
                _table(s, "rejected").pop(k, None)
                s.pop("refused", None)

            update_state(sent)
        before = state.get("refused")
        if refused and not (isinstance(before, dict) and before.get("reason") == refused):
            log(
                f"uploads refused by the server ({refused}): the queue stays until the key "
                "is approved for uploads (jezero device approve, without --read-only)"
            )

        def done(s: dict[str, Any]) -> None:
            s["last_flush"] = time.time()
            if refused:
                old = s.get("refused")
                same = isinstance(old, dict) and old.get("reason") == refused
                s["refused"] = old if same else {"reason": refused, "at": time.time()}

        update_state(done)
    return counts


# ---------------------------------------------------------------------------- hooks


def _read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def hook(event: str, agent: str, payload: dict[str, Any]) -> str:
    """Return the text to print (context for the model) or ''. Never raises."""
    cfg = load_config()
    session = str(payload.get("session_id") or "")
    cwd = payload.get("cwd")
    try:
        if event == "session-start":
            if not cfg:
                return ""  # no key: the jezero tools are not there, so say nothing
            out = [PROTOCOL]
            if uploads_enabled(cfg):
                spawn_flush()  # catch up sessions that never sent a session-end
            try:
                context = recent_context(cfg, cwd)
            except Exception as exc:  # noqa: BLE001 - never break the agent
                log(f"session-start context: {exc}")
                context = ""
            if context:
                out.append(context)
            return "\n\n".join(out)
        if event == "user-prompt":
            if not cfg or not session:
                return ""
            if session in _table(load_state(), "prompted"):
                return ""
            update_state(lambda s: _table(s, "prompted").__setitem__(session, time.time()))
            return prompt_context(cfg, str(payload.get("prompt") or ""), cwd)
        if event in ("stop", "session-end"):
            if not uploads_enabled(cfg):
                return ""
            transcript = payload.get("transcript_path")
            if agent == "hermes" and not transcript:
                exported = export_hermes(hermes_state_db(payload.get("hermes_home")), session)
                transcript = str(exported) if exported else None
            if not transcript:
                return ""
            if not enqueue(agent, session, str(transcript), cwd):
                return ""
            if event == "stop":
                last = _stamp(_table(load_state(), "stop_flush").get(session))
                if time.time() - last < STOP_THROTTLE_SECONDS:
                    return ""
                update_state(lambda s: _table(s, "stop_flush").__setitem__(session, time.time()))
            spawn_flush()
            return ""
    except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
        log(f"hook {event} {agent}: {exc!r}")
    return ""


_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def antigravity_prompt(transcript: str | None) -> str:
    """The latest request typed in an Antigravity conversation (its transcript.jsonl)."""
    if not transcript:
        return ""
    last = ""
    try:
        with Path(transcript).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"USER_INPUT"' not in line:
                    continue
                try:
                    step = json.loads(line)
                except ValueError:
                    continue
                if isinstance(step, dict) and step.get("type") == "USER_INPUT":
                    last = str(step.get("content") or "")
    except OSError:
        return ""
    found = _REQUEST.findall(last)
    return "\n\n".join(found).strip() if found else last.strip()


def antigravity_hook(event: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Antigravity hooks: JSON in (camelCase), JSON out. SessionStart and the first model
    call of a request get context as a system message; Stop queues the transcript."""
    workspaces = raw.get("workspacePaths") or []
    payload = {
        "session_id": raw.get("conversationId") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"),
        "cwd": workspaces[0] if workspaces and isinstance(workspaces[0], str) else None,
        "transcript_path": raw.get("transcriptPath"),
    }
    if event == "user-prompt":
        if raw.get("invocationNum") not in (0, None):
            return {}
        payload["prompt"] = antigravity_prompt(payload["transcript_path"])
    if event == "stop" and raw.get("fullyIdle") is False:
        return {}  # background work still running: wait for the final stop
    text = hook(event, "antigravity", payload)
    if text and event in ("session-start", "user-prompt"):
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}
    return {}


# ------------------------------------------------------------------------------ mcp


def _rpc_error(request_id: Any, message: str) -> str:
    error = {"code": -32000, "message": message}
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error})


def mcp_proxy(stdin: Any = None, stdout: Any = None) -> int:
    """A local stdio MCP server that forwards every message to the Jezero server with this
    computer's key, so an agent's config never holds the key (the Claude Code and
    Antigravity plugins, and what `setup` registers in Claude Code and Codex)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    session: str | None = None
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        batch = message if isinstance(message, list) else [message]
        ids = [m["id"] for m in batch if isinstance(m, dict) and "id" in m and "method" in m]
        cfg = load_config()
        replies: list[str] = []
        if cfg is None:
            hint = (
                f"Jezero is not connected on this computer (no key in {HOME}): "
                "run `jezero_client.py init`."
            )
            replies = [_rpc_error(i, hint) for i in ids]
        else:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if session:
                headers["Mcp-Session-Id"] = session
            received: dict[str, str] = {}
            try:
                status, raw = _http(
                    "POST",
                    mcp_url(cfg),
                    cfg["token"],
                    body=line.encode("utf-8"),
                    headers=headers,
                    timeout=60,
                    response_headers=received,
                )
            except (OSError, urllib.error.URLError) as exc:
                status, raw = 0, str(exc).encode()
            session = next(
                (v for k, v in received.items() if k.lower() == "mcp-session-id"), session
            )
            text = raw.decode("utf-8", "replace")
            if status >= 400 or status == 0:
                reason = f"Jezero is not reachable right now (HTTP {status}): {text[:200]}"
                replies = [_rpc_error(i, reason) for i in ids]
            elif text.lstrip().startswith(("event:", "data:")):
                replies = [d[5:].strip() for d in text.splitlines() if d.startswith("data:")]
            elif text.strip():
                replies = [text.strip()]
        for reply in replies:
            if reply:
                stdout.write(reply + "\n")
        stdout.flush()


# -------------------------------------------------------------------------- install


def init(
    url: str, device: str, force: bool, upload: bool = True, mcp_path_: str | None = None
) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", device):
        print("device: lowercase letters, digits, - or _", file=sys.stderr)
        return 2
    if load_config() and not force:
        print(f"already set up ({CONFIG}); use --force to make a new key", file=sys.stderr)
        return 1
    path = mcp_path(mcp_path_)
    url = url.rstrip("/")
    for suffix in dict.fromkeys((path, DEFAULT_MCP_PATH)):
        if url.endswith(suffix) and len(url) > len(suffix):
            url = url[: -len(suffix)]
            break
    token = secrets.token_urlsafe(32)
    _write_private(TOKEN_FILE, token)
    config: dict[str, Any] = {"url": url, "device": device}
    if path != DEFAULT_MCP_PATH:
        config["mcp_path"] = path
    if not upload:
        config["upload"] = False
    _save_json(CONFIG, config)
    digest = hashlib.sha256(token.encode()).hexdigest()
    print(
        f"Device key created. The key stays in {HOME}; "
        "the server only keeps its SHA-256 fingerprint."
    )
    if not upload:
        print("Read-only: this computer's sessions are not uploaded.")
    print("Ask the Jezero admin to approve it with:\n")
    flag = "" if upload else " --read-only"
    print(f"  jezero device approve {device} {digest}{flag}\n")
    return 0


def status() -> int:
    cfg = load_config()
    if not cfg:
        print(f"not set up ({HOME}): run `init` first")
        return 1
    print(f"key: {HOME}")
    if not uploads_enabled(cfg):
        try:
            sources = mcp_call(cfg, "list_sources", {}, 10)
        except Exception as exc:  # noqa: BLE001 - report any failure, never a traceback
            print(f"cannot read {cfg['url']}: {exc}")
            return 1
        count = len(sources.get("sources", [])) if isinstance(sources, dict) else 0
        print(f"server {cfg['url']}: read-only, {count} readable sources")
        return 0
    pending = len(list((SPOOL / "pending").glob("*.json")))
    state = load_state()
    rejected = sorted(
        (v for v in _table(state, "rejected").values() if isinstance(v, dict)),
        key=_stamp,
        reverse=True,
    )
    refused = state.get("refused")
    code = 1
    try:
        code, raw = _http(
            "GET", cfg["url"].rstrip("/") + "/inbox/v1/ping", cfg["token"], timeout=10
        )
        print(f"server {cfg['url']}: HTTP {code} {raw.decode('utf-8', 'replace')}")
    except (OSError, urllib.error.URLError) as exc:
        print(f"cannot reach {cfg['url']}: {exc}")
    print(f"queued sessions: {pending}")
    if isinstance(refused, dict) and pending:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(_stamp(refused)))
        print(f"uploads refused since {when}: {refused.get('reason')} (the queue is kept)")
    print(f"rejected uploads: {len(rejected)}")
    for entry in rejected[:5]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(_stamp(entry)))
        print(f"  {when} {entry.get('agent')}: {entry.get('reason')}")
    return 0 if code == 200 else 1


def _installed_core() -> Path:
    """A stable copy of this file for hooks outside a plugin folder (OpenCode)."""
    target = HOME / "core" / "jezero_client.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, str(target))
        projects = Path(__file__).with_name("jezero_projects.py")
        if projects.is_file():
            shutil.copyfile(str(projects), str(target.with_name("jezero_projects.py")))
    return target


def _bridge(agent: str) -> list[str]:
    """The command of the stdio MCP bridge (`mcp`), from a stable copy of this file."""
    return ["python3", str(_installed_core()), "mcp", "--agent", agent]


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def _claude_user_config() -> Path:
    """Where `claude mcp add --scope user` writes."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path("~").expanduser()) / ".claude.json"


def claude_plugin_installed() -> bool:
    """The Jezero plugin is installed in Claude Code (it brings its own MCP server)."""
    try:
        text = (_claude_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
    except OSError:
        return False
    return '"jezero@' in text


def _old_http_entry(entry: dict[str, Any]) -> bool:
    """An MCP entry that reaches Jezero over HTTP with the key in the agent's config."""
    return entry.get("type") in ("http", "sse") or "url" in entry or "headers" in entry


def _setup_claude() -> str:
    exe = shutil.which("claude") or str(Path("~/.local/bin/claude").expanduser())
    found = Path(exe).exists()
    data = _load_json(_claude_user_config(), {})
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get("jezero") if isinstance(servers, dict) else None
    entry = entry if isinstance(entry, dict) else None
    ours = entry is not None and (
        _old_http_entry(entry) or "jezero_client.py" in " ".join(map(str, entry.get("args") or []))
    )
    remove = [exe, "mcp", "remove", "jezero", "--scope", "user"]
    notes: list[str] = []
    if entry is not None and ours and (claude_plugin_installed() or _old_http_entry(entry)):
        what = (
            "old user-scope jezero HTTP entry (it held the key in clear)"
            if _old_http_entry(entry)
            else "user-scope jezero entry (the plugin's own server replaces it)"
        )
        if not found:
            return f"claude: remove the {what} with `claude mcp remove jezero --scope user`"
        done = subprocess.run(remove, capture_output=True, text=True)
        if done.returncode != 0:
            return f"claude: could not remove the {what}: {done.stderr[-300:]}"
        notes.append(f"removed the {what}")
        entry = None
    if claude_plugin_installed():
        notes.insert(0, "the Jezero plugin brings the MCP server; nothing to register")
        return "claude: " + "; ".join(notes)
    if not found:
        return "claude: not found, skipped"
    bridge = _bridge("claude-code")
    env = {"JEZERO_CLIENT_HOME": str(HOME)} if os.environ.get("JEZERO_CLIENT_HOME") else {}
    if entry is not None:
        same = [entry.get("command"), *(entry.get("args") or [])] == bridge
        if not ours or (same and (entry.get("env") or {}) == env):
            return "claude: MCP server jezero already registered"
        subprocess.run(remove, capture_output=True, text=True)
        notes.append("updated the jezero entry")
    flags = [w for k, v in env.items() for w in ("-e", f"{k}={v}")]
    result = subprocess.run(
        [exe, "mcp", "add", "--scope", "user", "jezero", *flags, "--", *bridge],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"claude: failed {result.stderr[-300:]}"
    notes.append(f"MCP server jezero registered (stdio bridge; the key stays in {HOME})")
    return "claude: " + "; ".join(notes)


def _toml_table(text: str, name: str) -> tuple[int, int] | None:
    """Where table `[name]` (with its sub-tables) sits in a TOML text, by characters."""
    header = re.search(rf"(?m)^\[{re.escape(name)}\][ \t]*(#.*)?$", text)
    if not header:
        return None
    for match in re.finditer(r"(?m)^\[\[?([^\]\n]+)\]", text[header.end() :]):
        if not match.group(1).strip().startswith(name + "."):
            return header.start(), header.end() + match.start()
    return header.start(), len(text)


def _setup_codex() -> str:
    """Register the stdio bridge in Codex' config.toml, so the key never sits there.
    Everything else in the file stays byte for byte."""
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    if not home.exists() and not shutil.which("codex"):
        return "codex: not found, skipped"
    path = home / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [
        "[mcp_servers.jezero]",
        'command = "python3"',
        "args = " + json.dumps(_bridge("codex")[1:]),
    ]
    if HOME.resolve() != Path("~/.jezero").expanduser().resolve():
        # Codex passes only a few variables to MCP servers: name the key's folder.
        lines.append(f"env = {{ JEZERO_CLIENT_HOME = {json.dumps(str(HOME))} }}")
    block = "\n".join(lines) + "\n"
    span = _toml_table(text, "mcp_servers.jezero")
    if span is None:
        sep = "" if not text else "\n" if text.endswith("\n") else "\n\n"
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(text + sep + block, encoding="utf-8")
        return f"codex: MCP server jezero added to config.toml (stdio bridge; key in {HOME})"
    start, end = span
    old = text[start:end]
    body = old.rstrip()
    if body + "\n" == block:
        return "codex: MCP server jezero already in config.toml"
    http = bool(re.search(r"(?m)^\s*(url|http_headers|bearer_token\w*)\s*=", body))
    if not http and "jezero_client.py" not in body:
        return "codex: MCP server jezero already in config.toml (not Jezero's bridge; left as is)"
    tail = old[len(body) :] or "\n"
    path.write_text(text[:start] + block.rstrip("\n") + tail + text[end:], encoding="utf-8")
    if http:
        return (
            "codex: replaced the old jezero entry in config.toml (it held the key in clear) "
            "with the stdio bridge"
        )
    return "codex: MCP server jezero updated in config.toml"


def _setup_opencode(plugin_source: Path | None) -> str:
    """The OpenCode plugin registers the MCP server itself (its `config` hook), so the
    user's opencode.json / opencode.jsonc is never edited."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "opencode"
    if not base.exists() and not shutil.which("opencode"):
        return "opencode: not found, skipped"
    if not plugin_source or not plugin_source.exists():
        return "opencode: pass --opencode-plugin PATH/jezero.ts to install the plugin"
    if plugin_source.is_dir():
        plugin_source = plugin_source / "jezero.ts"
    core = _installed_core()
    target = base / "plugins" / "jezero.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = plugin_source.read_text(encoding="utf-8").replace("__JEZERO_CORE__", str(core))
    target.write_text(text, encoding="utf-8")
    skill = plugin_source.parent / "skills" / "jezero" / "SKILL.md"
    if skill.is_file():
        skill_target = base / "skills" / "jezero" / "SKILL.md"
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(skill), str(skill_target))
    return f"opencode: plugin (with the MCP server and the skill) installed in {target}"


def setup(agents: list[str], opencode_plugin: str | None) -> int:
    """Register Jezero in each agent, with that agent's own key (see `agent_home`)."""
    original = HOME
    code = 0
    try:
        for agent in agents:
            use_agent_home(agent)
            if load_config() is None:
                print(f"{agent}: not set up ({HOME}): run `init` first", file=sys.stderr)
                code = 1
                continue
            if agent == "claude-code":
                print(_setup_claude())
            elif agent == "codex":
                print(_setup_codex())
            elif agent == "opencode":
                source = Path(opencode_plugin) if opencode_plugin else None
                print(_setup_opencode(source))
            elif agent == "antigravity":
                print(
                    "antigravity: install the plugin with `agy plugin install "
                    "https://github.com/MarsGotta/jezero/tree/main/plugins/antigravity` "
                    "(it brings the MCP server, the skill and the hooks)"
                )
            elif agent == "hermes":
                print(
                    "hermes: install the plugin with `hermes plugins install "
                    "MarsGotta/jezero/plugins/hermes --enable` (it brings its own tools)"
                )
    finally:
        use_home(original)
    return code


def _backfill_hermes(since: float, hermes_home: str | None) -> int:
    import sqlite3

    state_db = hermes_state_db(hermes_home)
    if not state_db.is_file():
        print(f"hermes: no state.db at {state_db}")
        return 0
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    try:
        ids = [
            r[0]
            for r in con.execute(
                "SELECT s.id FROM sessions s WHERE s.source NOT IN ('cron', 'subagent') AND "
                "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) >= ?",
                (since,),
            )
        ]
    finally:
        con.close()
    queued = 0
    for session_id in ids:
        path = export_hermes(state_db, session_id)
        if path and enqueue("hermes", session_id, str(path), None):
            queued += 1
    return queued


def _backfill_agent(agent: str, since: float, hermes_home: str | None) -> int:
    if agent == "hermes":
        return _backfill_hermes(since, hermes_home)
    queued = 0
    if agent == "antigravity":
        gemini = Path.home() / ".gemini"
        for app in ("antigravity-cli", "antigravity", "antigravity-ide"):
            pattern = "*/.system_generated/logs/transcript.jsonl"
            for path in sorted((gemini / app / "brain").glob(pattern)):
                conversation = path.parts[-4]
                try:
                    if path.stat().st_mtime >= since and enqueue(
                        agent, conversation, str(path), None
                    ):
                        queued += 1
                except OSError:
                    continue
        return queued
    roots = {
        "claude-code": (
            Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects",
            "*/*.jsonl",
        ),
        "codex": (
            Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions",
            "*/*/*/rollout-*.jsonl*",
        ),
    }
    if agent not in roots:
        print(f"{agent}: backfill not supported (sessions arrive through the plugin)")
        return 0
    root, pattern = roots[agent]
    for path in sorted(root.glob(pattern)):
        try:
            if path.stat().st_mtime >= since and enqueue(agent, path.stem, str(path), None):
                queued += 1
        except OSError:
            continue
    return queued


def backfill(agents: list[str], days: float, hermes_home: str | None = None) -> int:
    """Queue sessions already on disk (Claude Code, Codex, Hermes and Antigravity stores),
    each in the queue of that agent's own key (see `agent_home`)."""
    since = time.time() - days * 86400
    original = HOME
    queued: dict[Path, int] = {}
    try:
        for agent in agents:
            use_agent_home(agent)
            cfg = load_config()
            if cfg is None:
                print(f"{agent}: not set up ({HOME}): run `init` first")
                continue
            if not uploads_enabled(cfg):
                print(f"{agent}: read-only computer (init --no-upload): nothing to upload")
                continue
            queued[HOME] = queued.get(HOME, 0) + _backfill_agent(agent, since, hermes_home)
        if queued:
            print(f"queued {sum(queued.values())} sessions; uploading in the background")
        for home, count in queued.items():
            if count:
                use_home(home)
                spawn_flush()
    finally:
        use_home(original)
    return 0


# ------------------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jezero_client", description="Jezero client: shared core of the Jezero plugins."
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create this device's key")
    p.add_argument("--url", required=True, help="Jezero server, e.g. https://jezero.example.org")
    p.add_argument("--device", required=True, help="a short name for this computer")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="only read Jezero; never upload this computer's sessions",
    )
    p.add_argument(
        "--mcp-path",
        help="URL path of the server's MCP endpoint, when its admin changed it (default /mcp)",
    )
    p = sub.add_parser("status", help="check the key with the server and show the queue")
    p.add_argument("--agent", choices=AGENTS, help="the key this agent uses")
    p = sub.add_parser("setup", help="register the Jezero MCP server in the agents")
    p.add_argument("--agent", action="append", choices=SETUP_AGENTS)
    p.add_argument("--opencode-plugin", help="path to jezero.ts to install for OpenCode")
    p = sub.add_parser("mcp", help="stdio MCP server that forwards to Jezero with this key")
    p.add_argument("--agent", choices=AGENTS)
    p = sub.add_parser("hook", help="run a hook (reads the agent's JSON on stdin)")
    p.add_argument("event", choices=["session-start", "user-prompt", "stop", "session-end"])
    p.add_argument("--agent", required=True, choices=AGENTS)
    p = sub.add_parser("flush", help="upload queued sessions now")
    p.add_argument("--agent", choices=AGENTS, help="the queue of the key this agent uses")
    p = sub.add_parser("backfill", help="queue sessions already on this computer")
    p.add_argument("--agent", action="append", choices=AGENTS)
    p.add_argument("--days", type=float, default=90)
    p.add_argument("--hermes-home", help="Hermes profile folder (default: $HERMES_HOME)")
    sub.add_parser("protocol", help="print the instructions given to agents")
    args = parser.parse_args(argv)
    agent = getattr(args, "agent", None)
    use_agent_home(agent if isinstance(agent, str) else None)

    if args.command == "init":
        return init(
            args.url, args.device, args.force, upload=not args.no_upload, mcp_path_=args.mcp_path
        )
    if args.command == "status":
        return status()
    if args.command == "setup":
        return setup(args.agent or list(SETUP_AGENTS), args.opencode_plugin)
    if args.command == "mcp":
        return mcp_proxy()
    if args.command == "hook" and args.agent == "antigravity":
        try:
            out = antigravity_hook(args.event, _read_payload())
        except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
            log(f"hook {args.event} antigravity: {exc!r}")
            out = {}
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    if args.command == "hook":
        text = hook(args.event, args.agent, _read_payload())
        if text:
            sys.stdout.write(text + "\n")
        elif args.event == "stop" and args.agent == "codex":
            sys.stdout.write("{}\n")  # Codex expects JSON from Stop hooks
        return 0
    if args.command == "flush":
        cfg = load_config()
        if not cfg:
            return 1
        print(json.dumps(flush(cfg)))  # a detached flush writes to /dev/null
        return 0
    if args.command == "backfill":
        return backfill(args.agent or ["claude-code", "codex"], args.days, args.hermes_home)
    if args.command == "protocol":
        print(PROTOCOL)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
