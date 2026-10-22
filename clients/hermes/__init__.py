"""Jezero plugin for Hermes Agent: a thin adapter over the shared Python core.

- Tools `jezero_search`, `jezero_get_item`, `jezero_later`, `jezero_timeline` and
  `jezero_list_sources` call the Jezero MCP server with this computer's key. They only
  appear once the computer is connected (`core/jezero_client.py init`).
- First turn of a session: the Jezero protocol (how to use the tools by habit), recent
  work of the project and related past work, when it really matches, are added to the
  user's message (Hermes keeps the system prompt stable). Nothing without a key.
- End of a turn (at most every 10 minutes per session) and when a session closes: the
  session is exported from Hermes' state.db and uploaded in the background, unless the
  computer only reads Jezero (`init --no-upload`).
- The `jezero:jezero` skill explains how to search well.

Nothing here may slow down or break Hermes: network calls are short, hooks swallow their
errors, and uploads run in a detached process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "core"
_SKILL = _HERE / "skills" / "jezero" / "SKILL.md"
EXPORT_EVERY_SECONDS = 600
TOOL_TIMEOUT = 20.0

_core_module: Any = None
_core_lock = threading.Lock()
_last_export: dict[str, float] = {}
_protocol_given: dict[str, float] = {}


def _core() -> Any:
    """The shared client core shipped next to this file (imported once, lazily)."""
    global _core_module
    with _core_lock:
        if _core_module is None:
            if str(_CORE_DIR) not in sys.path:
                sys.path.insert(0, str(_CORE_DIR))
            import jezero_client

            _core_module = jezero_client
    return _core_module


def _config() -> dict[str, Any] | None:
    try:
        return _core().load_config()
    except Exception:  # noqa: BLE001 - a broken install must not break Hermes
        logger.debug("jezero: cannot load the client config", exc_info=True)
        return None


def _configured() -> bool:
    return _config() is not None


def _hermes_home() -> str | None:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes (tests) fall back to the environment
        return os.environ.get("HERMES_HOME")


def _cwd() -> str | None:
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------- tools

_HABIT = (
    " Jezero is the shared, read-only memory of past work of all of this user's agents"
    " (decisions, fixes, exact errors, commands and facts, each with its date)."
)
_ID = {"type": "integer", "description": "Item id, as returned by jezero_search."}
_SINCE = {"type": "string", "description": "ISO datetime with timezone; only newer items."}
TOOLS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "jezero_search": (
        "search",
        "Search Jezero."
        + _HABIT
        + " Use it out of habit, without being asked: before non-trivial work in a project,"
        " and when the user mentions something you have no context on. Query with 2-5"
        " distinctive words (names, ids, error text); matching is literal, so try a synonym"
        " if nothing comes back. Cite results like [123]; if nothing is found, say so.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "2-5 distinctive words."},
                "project": {"type": "string", "description": "Only this project."},
                "source": {"type": "string", "description": "Only this source."},
                "since": _SINCE,
                "limit": {"type": "integer", "description": "Maximum results."},
            },
            "required": ["query"],
        },
    ),
    "jezero_get_item": (
        "get_item",
        "Read one Jezero item in full (summary, atoms, date, project). Set include_original"
        " only when the summary is not enough.",
        {
            "type": "object",
            "properties": {
                "item_id": _ID,
                "include_original": {"type": "boolean", "description": "Add the source text."},
            },
            "required": ["item_id"],
        },
    ),
    "jezero_later": (
        "later",
        "Newer Jezero items on the same subject as item_id, newest first. Call it before"
        " relying on a decision, plan, status or number from Jezero: a later session may have"
        " changed it. Pass query with the words of what you are checking.",
        {
            "type": "object",
            "properties": {
                "item_id": _ID,
                "query": {"type": "string", "description": "Words of what you are checking."},
                "limit": {"type": "integer", "description": "Maximum results."},
            },
            "required": ["item_id"],
        },
    ),
    "jezero_timeline": (
        "timeline",
        "Jezero items in reverse chronological order, optionally by project, source and a"
        " [since, until) period: what happened in a project lately.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "source": {"type": "string"},
                "since": _SINCE,
                "until": {"type": "string", "description": "ISO datetime with timezone."},
                "limit": {"type": "integer"},
            },
        },
    ),
    "jezero_list_sources": (
        "list_sources",
        "Jezero sources you can read, with item counts and the latest date in each.",
        {"type": "object", "properties": {}},
    ),
}


def _tool_handler(remote: str):
    def handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        cfg = _config()
        if cfg is None:
            return json.dumps(
                {"error": "Jezero is not connected on this computer (run jezero_client.py init)."}
            )
        clean = {k: v for k, v in (args or {}).items() if v is not None and v != ""}
        try:
            data = _core().mcp_call(cfg, remote, clean, TOOL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report, never raise into the agent loop
            return json.dumps({"error": f"Jezero is not reachable right now: {exc}"})
        return json.dumps(data, ensure_ascii=False)

    return handler


# ----------------------------------------------------------------------------- hooks

_SENDER = re.compile(r"^\s*\[[^\]\n]{1,200}\]\s*")


def _pre_llm_call(
    session_id: str = "", user_message: str = "", is_first_turn: bool = False, **_: Any
) -> dict[str, str] | None:
    """First turn only: what Jezero already knows about this project and this request."""
    if not is_first_turn:
        return None
    cfg = _config()
    if cfg is None:
        return None
    core = _core()
    cwd = _cwd()
    parts: list[str] = []
    try:
        recent = core.recent_context(cfg, cwd)
        if recent:
            parts.append(recent)
        prompt = _SENDER.sub("", user_message or "")
        related = core.prompt_context(cfg, prompt, cwd)
        if related:
            parts.append(related)
    except Exception as exc:  # noqa: BLE001 - offline server: no context, no failure
        core.log(f"hermes pre_llm_call: {exc!r}")
    out: list[str] = []
    if _first_protocol(session_id):
        out.append(protocol())
    if parts:
        head = (
            "Jezero (shared memory of past work; check with jezero_get_item and jezero_later"
            " before relying on it):"
        )
        out += [head, *parts]
    return {"context": "\n\n".join(out)} if out else None


def protocol() -> str:
    """The core's protocol, with the names the tools have in Hermes."""
    return (
        _core()
        .PROTOCOL.replace("the jezero `search` tool", "the `jezero_search` tool")
        .replace("call `later` on", "call `jezero_later` on")
        .replace("`get_item`", "`jezero_get_item`")
    )


def _first_protocol(session_id: str) -> bool:
    """Once per session (a session id may come back after a context compression)."""
    if not session_id:
        return True
    if session_id in _protocol_given:
        return False
    _protocol_given[session_id] = time.time()
    if len(_protocol_given) > 500:
        for key in sorted(_protocol_given, key=lambda k: _protocol_given[k])[:-250]:
            _protocol_given.pop(key, None)
    return True


def _send(session_id: str) -> None:
    try:
        payload = {"session_id": session_id, "hermes_home": _hermes_home()}
        _core().hook("session-end", "hermes", payload)
    except Exception:  # noqa: BLE001
        logger.debug("jezero: upload of %s failed", session_id, exc_info=True)


def _queue(session_id: str | None, force: bool) -> None:
    if not session_id:
        return
    cfg = _config()
    if cfg is None or not _core().uploads_enabled(cfg):
        return
    now = time.time()
    if not force and now - _last_export.get(session_id, 0) < EXPORT_EVERY_SECONDS:
        return
    _last_export[session_id] = now
    if len(_last_export) > 500:
        for key in sorted(_last_export, key=lambda k: _last_export[k])[:-250]:
            _last_export.pop(key, None)
    threading.Thread(target=_send, args=(session_id,), name="jezero-upload", daemon=True).start()


def _on_session_end(session_id: str = "", **_: Any) -> None:
    _queue(session_id, force=False)


def _on_session_finalize(session_id: str | None = None, **_: Any) -> None:
    _queue(session_id, force=True)


# -------------------------------------------------------------------------- register


def register(ctx: Any) -> None:
    for name, (remote, description, parameters) in TOOLS.items():
        ctx.register_tool(
            name=name,
            toolset="jezero",
            schema={"name": name, "description": description, "parameters": parameters},
            handler=_tool_handler(remote),
            check_fn=_configured,
        )
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    if _SKILL.is_file():
        ctx.register_skill("jezero", _SKILL)
