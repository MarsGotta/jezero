"""Where the `jezero` app gets its data: the server (with this computer's key) or a demo.

Everything a screen shows goes through `Backend`, so the app, the pretty CLI and the tests
share one surface. `RemoteBackend` talks to the MCP server and to `/api/v1/overview` with
the key that `jezero connect` (or `jezero_client.py init`) created; `DemoBackend` serves
made-up data for screenshots and tests, with no network and no key.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .. import client_core as core

AGENT_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "hermes": "Hermes Agent",
    "antigravity": "Antigravity",
}
AGENT_BINARIES = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "hermes": "hermes",
    "antigravity": "agy",
}
REPO = "MarsGotta/jezero"
AGY_URL = f"https://github.com/{REPO}/tree/main/plugins/antigravity"
_HERE = Path(__file__).resolve().parent.parent
OPENCODE_PLUGIN = next(
    (
        p
        for p in (_HERE / "_plugins" / "opencode", _HERE.parent.parent / "plugins" / "opencode")
        if (p / "jezero.ts").is_file()
    ),
    _HERE / "_plugins" / "opencode",
)
"""The OpenCode plugin folder: inside the installed package, or in a source checkout."""

Step = tuple[list[str], bool]
"""A command, and whether the plan may go on when it fails (adding a marketplace that
is already there fails harmlessly)."""


class BackendError(RuntimeError):
    """A readable reason to show instead of data (not connected, server down...)."""


@dataclass
class AgentStatus:
    agent: str
    name: str
    present: bool
    """The agent's command is on this computer."""
    installed: bool
    """Jezero is installed in it (plugin, or MCP server and hooks)."""
    version: str | None
    latest: str
    where: str | None = None
    notes: list[str] = field(default_factory=list)
    last_seen: str | None = None
    """Newest session of this agent in Jezero (any computer), ISO datetime."""

    @property
    def outdated(self) -> bool:
        return bool(self.installed and self.version and _vkey(self.version) < _vkey(self.latest))

    @property
    def plan(self) -> list[Step]:
        """The commands that install Jezero in this agent, or update it when installed.
        They are the ones in docs/how-to/connect-agents.md."""
        setup = [sys.executable, str(Path(core.__file__)), "setup", "--agent", self.agent]
        update = self.installed
        if self.agent == "claude-code":
            if update:
                return [
                    (["claude", "plugin", "marketplace", "update", "jezero"], True),
                    (["claude", "plugin", "update", "jezero@jezero"], False),
                ]
            return [
                (["claude", "plugin", "marketplace", "add", REPO], True),
                (["claude", "plugin", "install", "jezero@jezero"], False),
                (setup, False),
            ]
        if self.agent == "codex":
            if update:
                return [
                    (["codex", "plugin", "marketplace", "upgrade", "jezero"], True),
                    (["codex", "plugin", "add", "jezero@jezero"], False),
                ]
            return [
                (["codex", "plugin", "marketplace", "add", REPO], True),
                (["codex", "plugin", "add", "jezero@jezero"], False),
                (setup, False),
            ]
        if self.agent == "opencode":
            return [([*setup, "--opencode-plugin", str(OPENCODE_PLUGIN)], False)]
        if self.agent == "hermes":
            if update:
                return [(["hermes", "plugins", "update", "jezero"], False)]
            return [(["hermes", "plugins", "install", f"{REPO}/plugins/hermes", "--enable"], False)]
        return [(["agy", "plugin", "install", AGY_URL], False)]

    @property
    def install_command(self) -> list[str]:
        """The plan as one line, for display."""
        return [w for i, (cmd, _) in enumerate(self.plan) for w in ([*(["&&"] if i else []), *cmd])]


class Backend(Protocol):
    label: str

    def overview(self, days: int = 14) -> dict[str, Any]: ...
    def search(self, query: str, project: str | None = None, limit: int = 20) -> list[dict]: ...
    def item(self, item_id: int, original: bool = False) -> dict[str, Any] | None: ...
    def later(self, item_id: int, query: str | None = None) -> tuple[list[dict], str]: ...
    def timeline(self, project: str | None = None, limit: int = 40) -> list[dict]: ...
    def agents(self) -> list[AgentStatus]: ...
    def context(self, project: str | None, prompt: str) -> dict[str, str]: ...


# ----------------------------------------------------------------------------- remote


class RemoteBackend:
    """The real server, read with this computer's key."""

    def __init__(self, cfg: dict[str, Any] | None = None, timeout: float = 15.0) -> None:
        cfg = cfg or core.load_config()
        if not cfg:
            raise BackendError(
                "This computer is not connected to Jezero yet: run `jezero connect`."
            )
        self.cfg = cfg
        self.timeout = timeout
        self.label = re.sub(r"^https?://", "", str(cfg["url"])).rstrip("/")
        self._sources: dict[str, Any] = {}

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        try:
            return core.mcp_call(self.cfg, tool, args, self.timeout)
        except Exception as exc:  # noqa: BLE001 - shown to the person, never a traceback
            raise BackendError(f"Jezero is not reachable right now ({exc}).") from exc

    def overview(self, days: int = 14) -> dict[str, Any]:
        url = f"{self.cfg['url'].rstrip('/')}/api/v1/overview?days={days}"
        try:
            status, raw = core._http("GET", url, self.cfg["token"], timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Jezero is not reachable right now ({exc}).") from exc
        if status == 200:
            data = json.loads(raw)
        elif status == 404:  # an older server: build what we can from list_sources
            sources = (self._call("list_sources", {}) or {}).get("sources") or {}
            data = {
                "device": self.cfg.get("device"),
                "upload": core.uploads_enabled(self.cfg),
                "items": sum(int(v.get("items", 0)) for v in sources.values()),
                "days": [],
                "sources": sources,
                "projects": {},
            }
        else:
            raise BackendError(f"The server answered HTTP {status}: is this key still approved?")
        self._sources = data.get("sources") or {}
        return data

    def search(self, query: str, project: str | None = None, limit: int = 20) -> list[dict]:
        args: dict[str, Any] = {"query": query, "limit": limit}
        if project:
            args["project"] = project
        return list((self._call("search", args) or {}).get("results") or [])

    def item(self, item_id: int, original: bool = False) -> dict[str, Any] | None:
        data = self._call("get_item", {"item_id": item_id, "include_original": original}) or {}
        return data.get("item") if data.get("found") else None

    def later(self, item_id: int, query: str | None = None) -> tuple[list[dict], str]:
        """Newer items, and how the server found them: "subject", or "project_timeline"
        when nothing later matched the subject and the project's newer items came back."""
        args: dict[str, Any] = {"item_id": item_id}
        if query:
            args["query"] = query
        data = self._call("later", args) or {}
        return list(data.get("later") or []), str(data.get("matched_by") or "subject")

    def timeline(self, project: str | None = None, limit: int = 40) -> list[dict]:
        args: dict[str, Any] = {"limit": limit}
        if project:
            args["project"] = project
        return list((self._call("timeline", args) or {}).get("items") or [])

    def agents(self) -> list[AgentStatus]:
        found = detect_agents()
        for status in found:
            status.last_seen = last_seen(self._sources, status.agent)
        return found

    def context(self, project: str | None, prompt: str) -> dict[str, str]:
        start = [core.PROTOCOL]
        if project:
            items = self.timeline(project, core.RECENT_ITEMS)
            if items:
                lines = [f"Recent work on project `{project}` in Jezero (newest first):"]
                start.append("\n".join([*lines, *(core._line(i) for i in items)]))
        first = ""
        terms = core.prompt_terms(prompt)
        if len(terms) >= 2:
            hits = [h for h in self.search(" ".join(terms), limit=8) if core.related(h, terms)]
            if hits:
                head = (
                    "Jezero has past work that may be related (check with `get_item` before using):"
                )
                first = "\n".join([head, *(core._line(h) for h in hits[: core.PROMPT_HITS])])
        return {"session_start": "\n\n".join(start), "first_prompt": first}


# ------------------------------------------------------------------------------ agents


def _vkey(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def _core_version(path: Path) -> str | None:
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("VERSION = "):
                return line.split('"')[1]
    except OSError:
        return None
    return None


def _newest_core(base: Path, pattern: str) -> tuple[str | None, Path | None]:
    best: tuple[tuple[int, ...], str, Path] | None = None
    for path in base.glob(pattern):
        version = _core_version(path)
        if version and (best is None or _vkey(version) > best[0]):
            best = (_vkey(version), version, path)
    return (best[1], best[2]) if best else (None, None)


def detect_agents(home: Path | None = None) -> list[AgentStatus]:
    """Which agents are on this computer and whether Jezero is installed in each."""
    home = home or Path.home()
    env = os.environ
    latest = core.VERSION
    out: list[AgentStatus] = []

    claude_dir = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    version, path = _newest_core(
        claude_dir / "plugins" / "cache" / "jezero", "*/*/core/jezero_client.py"
    )
    notes = []
    try:
        if '"jezero@' in (claude_dir / "plugins" / "installed_plugins.json").read_text():
            notes.append("MCP via plugin")
    except OSError:
        pass
    # User-scope MCP servers: $CLAUDE_CONFIG_DIR/.claude.json, else ~/.claude.json.
    claude_jsons = [home / ".claude.json"]
    if env.get("CLAUDE_CONFIG_DIR"):
        claude_jsons.insert(0, Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json")
    for claude_json in claude_jsons:
        try:
            entry = (json.loads(claude_json.read_text()).get("mcpServers") or {}).get("jezero")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(entry, dict):
            if entry.get("type") == "http" or "headers" in entry:
                notes.append("old MCP entry with the key in clear: run setup")
            elif "MCP via plugin" not in notes:
                notes.append("MCP server registered")
            break
    out.append(_status("claude-code", version, path, latest, notes))

    codex_dir = Path(env.get("CODEX_HOME") or home / ".codex")
    version, path = _newest_core(
        codex_dir / "plugins" / "cache", "**/jezero/**/core/jezero_client.py"
    )
    notes = []
    try:
        text = (codex_dir / "config.toml").read_text()
        if "[mcp_servers.jezero]" in text:
            block = text.split("[mcp_servers.jezero]", 1)[1].split("\n[", 1)[0]
            old = re.search(r"(?m)^\s*(url|http_headers)\s*=", block)
            notes.append(
                "old MCP entry with the key in clear: run setup" if old else "MCP server registered"
            )
        if version is None and "jezero" in text:
            version = "?"
    except OSError:
        pass
    out.append(_status("codex", version, path, latest, notes))

    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    plugin = xdg / "opencode" / "plugins" / "jezero.ts"
    version, path = (None, None)
    if plugin.is_file():
        core_file = (
            Path(env.get("JEZERO_CLIENT_HOME") or home / ".jezero") / "core" / "jezero_client.py"
        )
        version, path = (_core_version(core_file) or "?", plugin)
    out.append(_status("opencode", version, path, latest, []))

    hermes_home = Path(env.get("HERMES_HOME") or home / ".hermes")
    candidates = [
        hermes_home / "plugins" / "jezero",
        *(hermes_home / "profiles").glob("*/plugins/jezero"),
    ]
    version, path, notes = None, None, []
    for folder in candidates:
        v = _core_version(folder / "core" / "jezero_client.py")
        if v and (version is None or _vkey(v) > _vkey(version)):
            version, path = v, folder
    if len(candidates) > 1:
        profiles = [c.parent.parent.name for c in candidates[1:] if c.is_dir()]
        if profiles:
            notes.append("profiles: " + ", ".join(sorted(profiles)))
    out.append(_status("hermes", version, path, latest, notes))

    gemini = home / ".gemini" / "config"
    folder = gemini / "plugins" / "jezero"
    version = _core_version(folder / "core" / "jezero_client.py")
    notes = []
    if (gemini / "jezero" / "client.json").is_file():
        notes.append("own key in ~/.gemini/config/jezero")
    out.append(_status("antigravity", version, folder if version else None, latest, notes))
    return out


def _status(
    agent: str, version: str | None, path: Path | None, latest: str, notes: list[str]
) -> AgentStatus:
    if path is not None and path.name == "jezero_client.py":
        path = path.parent.parent  # the plugin folder, not its core file
    return AgentStatus(
        agent=agent,
        name=AGENT_NAMES[agent],
        present=shutil.which(AGENT_BINARIES[agent]) is not None,
        installed=version is not None,
        version=None if version == "?" else version,
        latest=latest,
        where=str(path) if path else None,
        notes=notes,
    )


def _agent_source(source: str, agent: str) -> bool:
    """`agent-<agent>-<device>` (uploads from a computer) or `agent-<agent>` (read on the
    server itself)."""
    return source == f"agent-{agent}" or source.startswith(f"agent-{agent}-")


def last_seen(sources: dict[str, Any], agent: str) -> str | None:
    dates = [
        str(v.get("latest"))
        for k, v in sources.items()
        if _agent_source(k, agent) and v.get("latest")
    ]
    return max(dates) if dates else None


def run_install(status: AgentStatus) -> tuple[bool, str]:
    """Install or update Jezero in one agent, step by step. Returns (ok, output)."""
    output: list[str] = []
    for command, may_fail in status.plan:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, "\n".join([*output, str(exc)])
        output.append((done.stdout + done.stderr).strip())
        if done.returncode != 0 and not may_fail:
            return False, "\n".join(output)
    return True, "\n".join(o for o in output if o)


# -------------------------------------------------------------------------------- demo

_DEMO_SOURCES = {
    "agent-claude-code-laptop": (412, [3, 5, 2, 0, 6, 9, 4, 7, 11, 8, 5, 12, 9, 14]),
    "agent-codex-laptop": (268, [1, 2, 4, 3, 0, 2, 5, 6, 3, 4, 8, 6, 7, 9]),
    "agent-hermes-server": (731, [8, 9, 7, 10, 12, 6, 9, 11, 13, 10, 12, 15, 11, 16]),
    "agent-opencode-laptop": (57, [0, 0, 1, 0, 2, 1, 0, 3, 1, 2, 0, 1, 2, 3]),
    "agent-antigravity-laptop": (23, [0, 0, 0, 0, 0, 0, 0, 1, 0, 2, 3, 1, 4, 5]),
    "tasks-board": (190, [2, 3, 1, 4, 2, 5, 3, 2, 6, 4, 3, 5, 7, 6]),
}
_DEMO_ITEMS = [
    {
        "id": 2412,
        "source": "agent-claude-code-laptop",
        "project": "atlas",
        "hours": 2,
        "title": "Uploader retry loop fixed: backoff now resets after a success",
        "summary": "Uploads of large session files stalled for hours after a single network "
        "blip. The retry delay doubled on every failure but never went back down, so one "
        "bad night left the queue waiting 6 hours between attempts. The fix resets the "
        "backoff after any successful upload and caps it at 10 minutes.",
        "atoms": [
            ("error", "uploader: next attempt in 21600s (queue: 14 files)"),
            ("decision", "Reset the backoff after any success; cap it at 600 seconds."),
            ("fix", "uploader.py: `delay = min(delay * 2, 600)` and `delay = 5` on success."),
            ("command", "pytest -q tests/test_uploader.py -k backoff"),
            ("rationale", "A stuck queue hides newer sessions from every agent."),
        ],
        "questions": ["Why did uploads stall for hours?", "What is the retry cap?"],
    },
    {
        "id": 2405,
        "source": "agent-hermes-server",
        "project": "billing",
        "hours": 7,
        "title": "Invoices keep the customer's tax id from the order, not the profile",
        "summary": "Customers who changed their tax id after paying got invoices with the new "
        "one. Invoices now copy the tax id at checkout time, matching what accounting "
        "expects.",
        "atoms": [
            ("decision", "Snapshot the tax id on the order at checkout."),
            ("risk", "Old invoices keep the profile value; regenerate on request only."),
            ("fact", "Accounting closes the month on the 3rd business day."),
        ],
        "questions": ["Which tax id goes on an invoice?"],
    },
    {
        "id": 2398,
        "source": "tasks-board",
        "project": "shop",
        "hours": 20,
        "title": "Analytics stays, loaded only after cookie consent",
        "summary": "The plan to remove analytics from the shop was reversed: it stays, "
        "self-hosted, and its script loads only after the visitor accepts the consent "
        "banner.",
        "atoms": [
            ("decision", "Keep analytics; load it only after consent."),
            ("fact", "Consent banner v2 ships with the next release."),
        ],
        "questions": ["Was analytics removed from the shop?"],
    },
    {
        "id": 2390,
        "source": "agent-codex-laptop",
        "project": "atlas",
        "hours": 29,
        "title": "Search ranks recent sessions higher with a 90-day half-life",
        "summary": "Two equally good matches now favour the newer one. Recency weighs 30% of "
        "the score and halves every 90 days.",
        "atoms": [
            ("decision", "Recency weight 0.3, half-life 90 days."),
            ("command", "jezero search 'retry backoff' --limit 5"),
        ],
        "questions": [],
    },
    {
        "id": 2377,
        "source": "agent-antigravity-laptop",
        "project": "shop",
        "hours": 46,
        "title": "Checkout page: 'Pay' button disabled until the address validates",
        "summary": "Orders with half-typed addresses failed at the carrier. The button now "
        "waits for the address check.",
        "atoms": [("fix", "checkout.tsx: disable Pay while `address.status !== 'valid'`.")],
        "questions": [],
    },
    {
        "id": 2361,
        "source": "agent-opencode-laptop",
        "project": "billing",
        "hours": 70,
        "title": "Monthly export moved to the 2nd at 06:00 UTC",
        "summary": "The export ran before late payments settled. It now runs a day later.",
        "atoms": [("decision", "Export on the 2nd at 06:00 UTC."), ("fact", "Takes ~4 min.")],
        "questions": [],
    },
]


class DemoBackend:
    """Made-up data: for screenshots, tests and a first look without a server."""

    label = "demo.jezero.local"

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 10, 21, 18, 30, tzinfo=UTC)

    def _item(self, raw: dict[str, Any]) -> dict[str, Any]:
        when = self.now - timedelta(hours=raw["hours"])
        return {
            "id": raw["id"],
            "source": raw["source"],
            "external_id": f"demo-{raw['id']}",
            "occurred_at": when.isoformat(),
            "title": raw["title"],
            "summary": raw["summary"],
            "questions": raw["questions"],
            "atoms": [{"kind": k, "text": t} for k, t in raw["atoms"]],
            "project": raw["project"],
            "account": None,
            "original_ref": None,
        }

    def overview(self, days: int = 14) -> dict[str, Any]:
        today = self.now.date()
        day_list = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
        sources = {
            name: {
                "items": total,
                "latest": (self.now - timedelta(hours=1 + i * 5)).isoformat(),
                "daily": daily[-days:],
            }
            for i, (name, (total, daily)) in enumerate(_DEMO_SOURCES.items())
        }
        projects = {
            "atlas": {"items": 524, "latest": (self.now - timedelta(hours=2)).isoformat()},
            "billing": {"items": 391, "latest": (self.now - timedelta(hours=7)).isoformat()},
            "shop": {"items": 288, "latest": (self.now - timedelta(hours=20)).isoformat()},
            "docs": {"items": 97, "latest": (self.now - timedelta(days=4)).isoformat()},
        }
        return {
            "ok": True,
            "device": "laptop",
            "upload": True,
            "version": "demo",
            "items": sum(t for t, _ in _DEMO_SOURCES.values()),
            "days": day_list,
            "sources": sources,
            "projects": projects,
        }

    def search(self, query: str, project: str | None = None, limit: int = 20) -> list[dict]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        hits = []
        for raw in _DEMO_ITEMS:
            item = self._item(raw)
            text = " ".join([item["title"], item["summary"], *(a["text"] for a in item["atoms"])])
            score = sum(text.lower().count(w) for w in words)
            if score and (project is None or item["project"] == project):
                hits.append({**item, "score": float(score)})
        return sorted(hits, key=lambda h: -h["score"])[:limit]

    def item(self, item_id: int, original: bool = False) -> dict[str, Any] | None:
        for raw in _DEMO_ITEMS:
            if raw["id"] == item_id:
                item = self._item(raw)
                if original:
                    item["original"] = "[demo] user: why do uploads stall?\nassistant: ..."
                return item
        return None

    def later(self, item_id: int, query: str | None = None) -> tuple[list[dict], str]:
        if item_id == 2390:
            return [self._item(_DEMO_ITEMS[0])], "subject"
        if item_id == 2377:  # nothing on its subject: the project's newer work, by date
            return [self._item(_DEMO_ITEMS[2])], "project_timeline"
        return [], "subject"

    def timeline(self, project: str | None = None, limit: int = 40) -> list[dict]:
        items = [self._item(r) for r in _DEMO_ITEMS if project in (None, r["project"])]
        return items[:limit]

    def agents(self) -> list[AgentStatus]:
        latest = core.VERSION
        rows = [
            ("claude-code", True, latest, ["MCP server registered"], 2),
            ("codex", True, "0.3.2", [], 29),
            ("opencode", True, latest, [], 70),
            ("hermes", True, latest, ["profiles: main"], 7),
            ("antigravity", False, None, [], 46),
        ]
        return [
            AgentStatus(
                agent=a,
                name=AGENT_NAMES[a],
                present=True,
                installed=installed,
                version=version,
                latest=latest,
                where=f"~/.{a}/plugins/jezero" if installed else None,
                notes=notes,
                last_seen=(self.now - timedelta(hours=hours)).isoformat(),
            )
            for a, installed, version, notes, hours in rows
        ]

    def context(self, project: str | None, prompt: str) -> dict[str, str]:
        start = [core.PROTOCOL]
        if project:
            items = self.timeline(project, core.RECENT_ITEMS)
            if items:
                lines = [f"Recent work on project `{project}` in Jezero (newest first):"]
                start.append("\n".join([*lines, *(core._line(i) for i in items)]))
        hits = self.search(prompt)[: core.PROMPT_HITS] if prompt.strip() else []
        first = ""
        if hits:
            head = "Jezero has past work that may be related (check with `get_item` before using):"
            first = "\n".join([head, *(core._line(h) for h in hits)])
        return {"session_start": "\n\n".join(start), "first_prompt": first}


# ------------------------------------------------------------------------------ helpers


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ago(value: Any, now: datetime | None = None, short: bool = False) -> str:
    """'just now', '5 min ago', '3 h ago', 'yesterday', '12 d ago', '3 mo ago'.
    `short`: 'now', '5m', '3h', '1d', '12d', '3mo' for narrow columns."""
    dt = parse_dt(value)
    if dt is None:
        return "never"
    seconds = ((now or datetime.now(UTC)) - dt).total_seconds()
    if short:
        for limit, unit, size in ((3600, "m", 60), (86400, "h", 3600), (60 * 86400, "d", 86400)):
            if seconds < limit:
                return "now" if seconds < 60 else f"{int(seconds // size)}{unit}"
        return f"{int(seconds // (30 * 86400))}mo"
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 60 * 86400:
        return f"{int(seconds // 86400)} d ago"
    return f"{int(seconds // (30 * 86400))} mo ago"


def source_agent(source: str) -> str | None:
    """`agent-claude-code-laptop` (or `agent-claude-code`) -> `claude-code`."""
    for agent in sorted(AGENT_NAMES, key=len, reverse=True):
        if _agent_source(source, agent):
            return agent
    return None
