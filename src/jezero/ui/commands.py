"""Commands for people, printed with care: `jezero find`, `show`, `agents`, `connect`.

They read Jezero with this computer's key, like the app and the agents do; `--demo` runs
them on made-up data. Admin commands (`jezero run`, `device approve`...) live in `cli.py`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .. import client_core as core
from .data import Backend, BackendError, DemoBackend, RemoteBackend, ago, source_agent
from .look import AGENT_GLYPHS, NIGHT, chip, project_color, wordmark

P = NIGHT


def _backend(args: argparse.Namespace) -> Backend:
    return DemoBackend() if getattr(args, "demo", False) else RemoteBackend()


def _console() -> Console:
    return Console(highlight=False)


def cmd_find(args: argparse.Namespace) -> int:
    console = _console()
    backend = _backend(args)
    hits = backend.search(" ".join(args.query), args.project, args.limit)
    if not hits:
        console.print(
            Text("Nothing in Jezero for that. Matching is literal: try a synonym.", style=P.muted)
        )
        return 1
    for hit in hits:
        line = Text()
        line.append_text(chip(f"#{hit.get('id')}", P.sand, P))
        line.append(" ")
        line.append(str(hit.get("title")), style=f"bold {P.foreground}")
        meta = Text("      ")
        agent = source_agent(str(hit.get("source") or ""))
        meta.append(f"{AGENT_GLYPHS.get(agent or '', '▪')} ", style=P.muted)
        if hit.get("project"):
            meta.append(str(hit["project"]), style=project_color(str(hit["project"]), P))
            meta.append(" · ", style=P.faint)
        meta.append(ago(hit.get("occurred_at")), style=P.muted)
        console.print(line)
        console.print(meta)
    console.print(
        Text(f"\n{len(hits)} results · jezero show ID for the whole memory", style=P.muted)
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .app import item_view

    console = _console()
    backend = _backend(args)
    item = backend.item(args.id)
    if item is None:
        console.print(Text(f"No memory #{args.id} that this key can read.", style=P.error))
        return 1
    newer = backend.later(args.id) if args.newer else None
    hint = f"add --newer to check whether something newer changed #{args.id}"
    view = item_view(item, P, newer, hint=hint)
    console.print(Panel(view, border_style=P.faint, padding=(1, 2)))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    console = _console()
    backend = _backend(args)
    try:
        backend.overview(14)
    except BackendError as exc:
        console.print(Text(str(exc), style=P.muted))
    table = Table(box=None, padding=(0, 2), show_edge=False, header_style=f"bold {P.muted}")
    for column in ("", "agent", "jezero", "last session", "plugin"):
        table.add_column(column)
    for s in backend.agents():
        if s.installed and not s.outdated:
            state = Text(f"● v{s.version}", style=P.success)
        elif s.outdated:
            state = Text(f"▲ v{s.version} → v{s.latest}", style=P.warning)
        elif s.present:
            state = Text("○ not installed", style=P.muted)
        else:
            state = Text("○ not on this computer", style=P.faint)
        table.add_row(
            Text(AGENT_GLYPHS.get(s.agent, "▪"), style=P.teal),
            Text(s.name, style=f"bold {P.foreground}"),
            state,
            Text(ago(s.last_seen) if s.last_seen else "none yet", style=P.muted),
            Text(s.where or "", style=P.muted),
        )
    console.print(table)
    console.print(Text("\nInstall or update from the app: jezero ui, then 4.", style=P.muted))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    console = _console()
    console.print(wordmark(P))
    console.print(Text("\nConnect this computer to Jezero\n", style=f"bold {P.foreground}"))
    if core.load_config() and not args.force:
        cfg: dict[str, Any] = core.load_config() or {}
        console.print(
            Text(f"Already connected to {cfg.get('url')} as {cfg.get('device')}.", style=P.muted)
        )
        console.print(Text("Use --force to create a new key.", style=P.muted))
        return 0
    url = args.url or Prompt.ask(Text("Server", style=P.ochre), default="https://")
    device = args.device or Prompt.ask(Text("Name of this computer", style=P.ochre))
    read_only = args.read_only or Confirm.ask(
        Text("Read only? (search Jezero, never upload this computer's sessions)", style=P.ochre),
        default=True,
    )
    return core.init(url.strip(), device.strip().lower(), force=args.force, upload=not read_only)


def cmd_ui(args: argparse.Namespace) -> int:
    from .app import run

    return run(demo=args.demo)


def add_commands(sub: Any) -> None:
    """Register the people commands on the main `jezero` parser."""
    p = sub.add_parser("ui", help="open the Jezero app (also: `jezero` with no command)")
    p.add_argument("--demo", action="store_true", help="made-up data, no server needed")
    p.set_defaults(fn=lambda _c, a: cmd_ui(a), needs_config=False)

    p = sub.add_parser("find", help="search Jezero with this computer's key, like an agent")
    p.add_argument("query", nargs="+", help="2 to 5 distinctive words")
    p.add_argument("--project", help="only this project")
    p.add_argument("--limit", type=int, default=10, help="maximum results")
    p.add_argument("--demo", action="store_true", help="made-up data, no server needed")
    p.set_defaults(fn=lambda _c, a: _guard(cmd_find, a), needs_config=False)

    p = sub.add_parser("show", help="one memory in full; --newer checks if it changed later")
    p.add_argument("id", type=int, help="item id, as shown by `find` (#123)")
    p.add_argument("--newer", action="store_true", help="also list newer items on the subject")
    p.add_argument("--demo", action="store_true", help="made-up data, no server needed")
    p.set_defaults(fn=lambda _c, a: _guard(cmd_show, a), needs_config=False)

    p = sub.add_parser("agents", help="which agents on this computer use Jezero")
    p.add_argument("--demo", action="store_true", help="made-up data, no server needed")
    p.set_defaults(fn=lambda _c, a: _guard(cmd_agents, a), needs_config=False)

    p = sub.add_parser("connect", help="create this computer's key (asks what it needs)")
    p.add_argument("--url", help="the server, https://YOUR-JEZERO (asked if missing)")
    p.add_argument("--device", help="a name for this computer (asked if missing)")
    p.add_argument("--read-only", action="store_true", help="search only, never upload")
    p.add_argument("--force", action="store_true", help="replace this computer's key")
    p.set_defaults(fn=lambda _c, a: cmd_connect(a), needs_config=False)


def _guard(fn: Any, args: argparse.Namespace) -> int:
    try:
        return int(fn(args))
    except BackendError as exc:
        print(str(exc), file=sys.stderr)
        return 2
