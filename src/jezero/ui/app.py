"""`jezero`: a terminal app to look after Jezero and search it the way an agent does.

Five views, one key each: Home (what Jezero holds and how alive each source is), Search,
Timeline, Agents (which agents on this computer have Jezero, install or update them) and
Agent view (the exact context an agent receives). `jezero ui --demo` runs it on made-up
data, with no server and no key.
"""

from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Digits,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    Switch,
    Tab,
    Tabs,
)
from textual.widgets.option_list import Option

from .. import client_core as core
from .data import (
    AGENT_NAMES,
    AgentStatus,
    Backend,
    BackendError,
    DemoBackend,
    RemoteBackend,
    ago,
    parse_dt,
    run_install,
    source_agent,
)
from .look import (
    AGENT_GLYPHS,
    ATOMS,
    PALETTES,
    THEMES,
    Palette,
    atom_color,
    chip,
    gradient,
    project_color,
    sparkline,
    wordmark,
)
from .safe import CleanBackend, clean_tree, unwrap

CSS = """
Screen { background: $background; }

#topbar { height: 3; padding: 1 2 0 2; background: $background; }
#brand { width: auto; padding-right: 3; }
#tabs { width: 1fr; height: 2; background: $background; }
Tabs .underline--bar { color: $primary; background: $panel; }
Tab { color: $text-muted; padding: 0 2; }
Tab.-active { color: $foreground; text-style: bold; }
Tab:hover { color: $foreground; }
#where { width: auto; padding-left: 2; color: $text-muted; }

#views { height: 1fr; }
.pane { height: 1fr; padding: 1 2 0 2; }
.panel {
    border: round $border; border-title-color: $text-muted; border-title-style: bold;
    border-subtitle-color: $text-muted; background: $surface; padding: 0 1;
}
.panel:focus-within { border: round $primary 70%; border-title-color: $primary; }

/* Home */
#hero { height: 9; }
#wordmark { width: 54; height: 9; padding: 0 0 0 1; }
#tiles { grid-size: 4 1; grid-gutter: 0 2; height: 9; width: 1fr; padding: 1 0 0 2; }
.tile { border: round $border; background: $surface; padding: 0 1; height: 7; }
.tile Label { color: $text-muted; text-style: bold; width: 1fr; }
.tile Digits { color: $primary; width: 1fr; text-style: bold; }
.tile .caption { color: $text-muted; text-style: not bold; }
#home-body { height: 1fr; min-height: 22; margin-top: 1; }
#home-left { width: 3fr; height: 1fr; }
#home-charts { height: auto; }
#activity { width: auto; height: auto; }
#shares { width: 1fr; height: auto; margin-left: 2; }
#bars { width: auto; height: auto; padding: 1 1 0 1; }
#shares-body { height: auto; padding: 1 1 0 1; }
#sources-panel { height: 1fr; margin-top: 1; }
#sources { height: auto; padding: 1 0 0 0; }
#latest { width: 2fr; height: 1fr; margin-left: 2; }

OptionList { background: $surface; border: none; padding: 0; }
OptionList:focus { background-tint: transparent; }
OptionList > .option-list--option-highlighted { background: $panel; }
OptionList:focus > .option-list--option-highlighted { background: $panel; text-style: none; }
OptionList > .option-list--separator { color: $border; }

/* Search */
#search-bar { height: 3; }
#search-icon { width: 4; padding: 1 0 0 2; color: $primary; text-style: bold; }
#query { width: 1fr; border: tall $border; background: $surface; }
#query:focus { border: tall $primary; }
#search-body { height: 1fr; margin-top: 1; }
#results { width: 2fr; height: 1fr; }
#detail { width: 3fr; height: 1fr; margin-left: 2; }
#detail-body { padding: 1 1; }

/* Timeline */
#projects { width: 30; height: 1fr; }
#timeline { width: 1fr; height: 1fr; margin-left: 2; }

/* Agents */
#computer { height: auto; margin-bottom: 1; }
#computer-body { padding: 1 1; height: auto; }
#agent-grid { grid-size: 3; grid-gutter: 1 2; grid-rows: 11; height: auto; }
.agent { height: 11; border: round $border; background: $surface; padding: 0 1; }
.agent.-ok { border: round $success 60%; }
.agent.-outdated { border: round $warning 80%; }
.agent.-missing { border: round $primary 35%; }
.agent .agent-body { height: 1fr; padding: 1 1 0 1; }
.agent .agent-actions { height: 1; align-horizontal: right; }
.agent Button { width: auto; min-width: 12; margin: 0 1 0 0; }
Button { border: tall $border; background: $panel; color: $foreground; }
Button.-primary {
    background: $primary; color: $background; border: tall $primary; text-style: bold;
}
Button:hover { background: $primary 80%; color: $background; }
Button.-textual-compact { border: none; padding: 0 2; }

/* Agent view */
#ctx-bar { height: 3; }
#ctx-project { width: 32; }
#ctx-prompt { width: 1fr; margin-left: 2; border: tall $border; background: $surface; }
#ctx-prompt:focus { border: tall $primary; }
#ctx-body { height: 1fr; margin-top: 1; }
#ctx-start, #ctx-first { width: 1fr; height: 1fr; }
#ctx-first { margin-left: 2; }
.ctx-text { padding: 1 1; }
Select > SelectCurrent { border: tall $border; background: $surface; }
Select:focus > SelectCurrent { border: tall $primary; }

/* Modals */
ModalScreen { align: center middle; background: $background 70%; }
#modal {
    width: 90; height: auto; max-height: 90%; border: round $primary; background: $surface;
    padding: 1 2;
}
#modal-body { height: auto; max-height: 30; }
#modal-buttons { height: 3; margin-top: 1; align-horizontal: right; }
#modal-buttons Button { margin-left: 2; }
#connect Input { border: tall $border; background: $panel; margin-bottom: 1; }
#connect Input:focus { border: tall $primary; }
#connect Label { color: $text-muted; }
#connect Horizontal { height: 3; }

Footer { background: $background; }
"""


def _when(value: Any) -> str:
    dt = parse_dt(value)
    return dt.astimezone().strftime("%b %d, %H:%M") if dt else ""


def _int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


# --------------------------------------------------------------------------- renderers


def item_view(
    item: dict[str, Any],
    p: Palette,
    newer: list[dict] | None = None,
    hint: str | None = None,
    matched_by: str = "subject",
) -> Group:
    """One memory: title, where it comes from, summary, atoms, questions, newer items.
    `matched_by` is how the server found them: "project_timeline" means nothing later
    matched the subject, and they are only the project's newer items, by date."""
    item = clean_tree(item)
    newer = clean_tree(newer)
    parts: list[Any] = []
    parts.append(Text(str(item.get("title") or "(untitled)"), style=f"bold {p.foreground}"))
    meta = Text()
    meta.append_text(chip(f"#{item.get('id')}", p.sand, p))
    meta.append("  ")
    if item.get("project"):
        meta.append_text(chip(str(item["project"]), project_color(str(item["project"]), p), p))
        meta.append("  ")
    agent = source_agent(str(item.get("source") or ""))
    glyph = AGENT_GLYPHS.get(agent or "", "▪")
    meta.append(f"{glyph} {item.get('source')}", style=p.muted)
    meta.append(
        f"   {_when(item.get('occurred_at'))} · {ago(item.get('occurred_at'))}", style=p.muted
    )
    parts += [Text(), meta, Text()]
    parts.append(inline_code(str(item.get("summary") or ""), p.foreground, p.teal))
    atoms = item.get("atoms") or []
    if atoms:
        parts += [Text(), Text("WHAT IT HOLDS", style=f"bold {p.muted}")]
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2)
        grid.add_column(ratio=1)
        for atom in atoms:
            kind = str(atom.get("kind"))
            icon, _ = ATOMS.get(kind, ("•", kind))
            color = atom_color(kind, p)
            text = inline_code(str(atom.get("text") or ""), p.foreground, p.teal)
            if kind == "command":
                text.stylize(p.teal)
            grid.add_row(Text(icon, style=f"bold {color}"), text)
        parts.append(Padding(grid, (1, 0, 0, 0)))
    questions = item.get("questions") or []
    if questions:
        parts += [Text(), Text("ANSWERS QUESTIONS LIKE", style=f"bold {p.muted}"), Text()]
        for q in questions[:5]:
            parts.append(Text.assemble(("? ", f"bold {p.teal}"), (str(q), p.foreground)))
    if newer is not None:
        by_date = matched_by == "project_timeline" and bool(newer)
        title = (
            "NEWER IN THIS PROJECT (by date, not by subject)"
            if by_date
            else "NEWER ON THIS SUBJECT"
        )
        parts += [Text(), Text(title, style=f"bold {p.muted}"), Text()]
        if not newer:
            parts.append(Text("✔ Nothing newer: this is the latest word.", style=p.success))
        for n in newer:
            parts.append(
                Text.assemble(
                    ("↳ ", f"bold {p.sand}"),
                    (f"#{n.get('id')} ", p.sand),
                    (str(n.get("title")), p.foreground),
                    (f"  {ago(n.get('occurred_at'))}", p.muted),
                )
            )
    elif item.get("id") is not None:
        hint = hint or "press  l  to check whether something newer changed this"
        parts += [Text(), Text(hint, style=p.muted)]
    return Group(*parts)


def result_line(item: dict[str, Any], p: Palette, width: int = 60) -> Text:
    item = clean_tree(item)
    title = str(item.get("title") or "")
    if len(title) > width:
        title = title[: width - 1] + "…"
    line = Text(title, style=f"bold {p.foreground}", no_wrap=True, overflow="ellipsis")
    line.append("\n")
    agent = source_agent(str(item.get("source") or ""))
    line.append(f"{AGENT_GLYPHS.get(agent or '', '▪')} ", style=p.muted)
    if item.get("project"):
        line.append(str(item["project"]), style=project_color(str(item["project"]), p))
        line.append(" · ", style=p.faint)
    line.append(ago(item.get("occurred_at")), style=p.muted)
    line.append(f"  #{item.get('id')}", style=p.faint)
    return line


def bars(days: list[str], totals: list[int], p: Palette, height: int = 6) -> Text:
    """A small column chart of memories per day, with eighth-block tops."""
    top = max(totals) if totals and max(totals) > 0 else 1
    out = Text(no_wrap=True)
    for row in range(height, 0, -1):
        for i, v in enumerate(totals):
            filled = v / top * height
            if filled >= row:
                ch = "██"
            elif filled > row - 1:
                ch = " ▁▂▃▄▅▆▇█"[max(1, round((filled - (row - 1)) * 8))] * 2
            else:
                ch = "  "
            color = gradient([p.rust, p.ochre, p.sand], (row - 1) / max(1, height - 1))
            if i == len(totals) - 1 and ch.strip():
                color = p.teal
            out.append(ch, style=color)
            out.append(" ")
        out.append("\n")
    for i, day in enumerate(days):
        try:
            label = datetime.fromisoformat(day).strftime("%a")[:2]
        except ValueError:
            label = "  "
        out.append(label, style=p.teal if i == len(days) - 1 else p.muted)
        out.append(" ")
    return out


def sources_table(sources: dict[str, Any], p: Palette, now: datetime) -> Table:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(width=1)
    table.add_column(ratio=3, no_wrap=True)
    table.add_column(width=14, no_wrap=True)
    table.add_column(justify="right", width=6)
    table.add_column(justify="right", width=12, no_wrap=True)
    head = f"bold {p.muted}"
    table.add_row(
        Text(""),
        Text("SOURCE", style=head),
        Text("14 DAYS", style=head),
        Text("ITEMS", style=head),
        Text("NEWEST", style=head),
    )
    rows = sorted(sources.items(), key=lambda kv: str(kv[1].get("latest") or ""), reverse=True)
    for name, info in rows:
        latest = parse_dt(info.get("latest"))
        age = (now - latest).total_seconds() if latest else 1e12
        dot = p.success if age < 86400 else p.sand if age < 7 * 86400 else p.faint
        agent = source_agent(name)
        label = Text(f"{AGENT_GLYPHS.get(agent or '', '▪')} ", style=p.muted)
        label.append(name, style=p.foreground)
        table.add_row(
            Text("●", style=dot),
            label,
            sparkline(list(info.get("daily") or []), p, 14),
            Text(_int(info.get("items")), style=f"bold {p.foreground}"),
            Text(ago(info.get("latest"), now), style=p.muted),
        )
    return table


def project_shares(projects: dict[str, Any], p: Palette, now: datetime, rows: int = 7) -> Table:
    """Each project's share of all memories, as a bar, newest activity first."""
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(no_wrap=True, max_width=16)
    table.add_column(ratio=1, no_wrap=True, overflow="crop")
    table.add_column(justify="right", width=4)
    table.add_column(justify="right", no_wrap=True, width=4)
    items = list(projects.items())
    total = sum(int(v.get("items") or 0) for _, v in items) or 1
    biggest = max((int(v.get("items") or 0) for _, v in items), default=1) or 1
    for name, info in items[:rows]:
        n = int(info.get("items") or 0)
        color = project_color(name, p)
        width = 16
        filled = n / biggest * width
        bar = Text("█" * int(filled), style=color)
        part = filled - int(filled)
        if part > 0.1 and int(filled) < width:
            bar.append("▏▎▍▌▋▊▉"[min(6, int(part * 7))], style=color)
        bar.append("·" * max(0, width - len(bar)), style=p.faint)
        table.add_row(
            Text.assemble(("● ", color), (name, p.foreground)),
            bar,
            Text(f"{n / total:.0%}", style=p.muted),
            Text(ago(info.get("latest"), now, short=True), style=p.muted),
        )
    if len(items) > rows:
        table.add_row(Text(f"+{len(items) - rows} more", style=p.muted), Text(), Text(), Text())
    return table


def inline_code(text: str, base: str, code: str) -> Text:
    """Plain text with `code` spans coloured, backticks removed."""
    out = Text(style=base)
    for i, part in enumerate(text.split("`")):
        out.append(part, style=code if i % 2 else base)
    return out


# ------------------------------------------------------------------------------- tiles


class Tile(Vertical):
    def __init__(self, label: str, tile_id: str) -> None:
        super().__init__(classes="tile", id=tile_id)
        self.label = label

    def compose(self) -> ComposeResult:
        yield Label(self.label.upper())
        yield Digits("–")
        yield Label("", classes="caption")

    def set(self, value: str, caption: str) -> None:
        self.query_one(Digits).update(value)
        self.query_one(".caption", Label).update(caption)


class AgentCard(Vertical):
    def __init__(self, status: AgentStatus) -> None:
        super().__init__(classes="agent", id=f"agent-{status.agent}")
        self.status = status

    def compose(self) -> ComposeResult:
        yield Static(id=f"agent-body-{self.status.agent}", classes="agent-body")
        with Horizontal(classes="agent-actions"):
            yield Button("", id=f"install-{self.status.agent}", compact=True)

    def on_mount(self) -> None:
        self.refresh_card(self.status)

    def refresh_card(self, s: AgentStatus) -> None:
        self.status = s
        p = self.app.palette  # type: ignore[attr-defined]
        self.border_title = f"{AGENT_GLYPHS.get(s.agent, '▪')} {s.name}"
        self.set_class(s.installed and not s.outdated, "-ok")
        self.set_class(s.outdated, "-outdated")
        self.set_class(not s.installed, "-missing")
        body = Text()
        if s.installed and not s.outdated:
            body.append("● connected", style=f"bold {p.success}")
            body.append(f"   v{s.version}" if s.version else "", style=p.muted)
        elif s.outdated:
            body.append("▲ update ready", style=f"bold {p.warning}")
            body.append(f"   v{s.version} → v{s.latest}", style=p.muted)
        elif s.present:
            body.append("○ not installed", style=f"bold {p.muted}")
        else:
            body.append("○ not on this computer", style=p.faint)
        body.append("\n\n")
        body.append("last session  ", style=p.muted)
        body.append(ago(s.last_seen) if s.last_seen else "none yet", style=p.foreground)
        if s.where:
            home = str(Path.home())
            where = s.where.replace(home, "~", 1) if s.where.startswith(home) else s.where
            body.append("\nplugin        ", style=p.muted)
            body.append(where, style=p.foreground)
        for note in s.notes[:2]:
            body.append(f"\n{note}", style=p.muted)
        self.query_one(".agent-body", Static).update(body)
        button = self.query_one(Button)
        button.label = "Update" if s.outdated else "Reinstall" if s.installed else "Install"
        button.set_class(not s.installed or s.outdated, "-primary")
        button.disabled = not s.present


# ------------------------------------------------------------------------------ modals


class ItemScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "dismiss", "Close"), Binding("l", "newer", "Newer?")]

    def __init__(self, backend: Backend, item: dict[str, Any]) -> None:
        super().__init__()
        self.backend = backend
        self.item = item

    def compose(self) -> ComposeResult:
        palette: Palette = self.app.palette  # type: ignore[attr-defined]
        with Vertical(id="modal"), VerticalScroll(id="modal-body"):
            yield Static(item_view(self.item, palette), id="modal-item")

    @work(thread=True, exclusive=True)
    def action_newer(self) -> None:
        try:
            newer, how = self.backend.later(int(self.item["id"]), None)
        except BackendError as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        view = item_view(self.item, self.app.palette, newer, matched_by=how)  # type: ignore[attr-defined]
        self.app.call_from_thread(self.query_one("#modal-item", Static).update, view)


class ConfirmInstall(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, status: AgentStatus) -> None:
        super().__init__()
        self.status = status

    def compose(self) -> ComposeResult:
        p: Palette = self.app.palette  # type: ignore[attr-defined]
        s = self.status
        verb = "Update" if s.outdated else "Reinstall" if s.installed else "Install"
        text = Text()
        text.append(f"{verb} Jezero in {s.name}\n\n", style=f"bold {p.foreground}")
        text.append(
            "These commands run in order and change the agent's configuration on this\n"
            "computer: the Jezero plugin, its MCP server and its hooks. Nothing else.\n\n",
            style=p.muted,
        )
        for command, _ in s.plan:
            text.append("❯ ", style=f"bold {p.teal}")
            text.append(" ".join(command) + "\n", style=p.teal)
        with Vertical(id="modal"):
            yield Static(text)
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="no")
                yield Button(verb, id="yes", classes="-primary")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ConnectScreen(ModalScreen[bool]):
    """First run: create this computer's key and show what the admin must approve."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def compose(self) -> ComposeResult:
        p: Palette = self.app.palette  # type: ignore[attr-defined]
        intro = Text()
        intro.append("Connect this computer to Jezero\n\n", style=f"bold {p.foreground}")
        intro.append(
            "Jezero creates a key and keeps it on this computer. The server only keeps its "
            "SHA-256 fingerprint, which its admin approves once.\n",
            style=p.muted,
        )
        with Vertical(id="modal"), Vertical(id="connect"):
            yield Static(intro)
            yield Label("Server")
            yield Input(placeholder="https://your-jezero", id="url")
            yield Label("Name of this computer")
            yield Input(placeholder="laptop", id="device")
            with Horizontal():
                yield Switch(value=True, id="read-only")
                yield Label("  Read only (do not upload this computer's sessions)")
            yield Static(id="connect-result")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="no")
                yield Button("Create key", id="yes", classes="-primary")

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def create(self) -> None:
        p: Palette = self.app.palette  # type: ignore[attr-defined]
        url = self.query_one("#url", Input).value.strip()
        device = self.query_one("#device", Input).value.strip().lower()
        if not url.startswith(("http://", "https://")) or not device:
            self.query_one("#connect-result", Static).update(
                Text("A server address (https://...) and a name are needed.", style=p.error)
            )
            return
        upload = not self.query_one("#read-only", Switch).value
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = core.init(url, device, force=False, upload=upload)
        text = Text(out.getvalue().strip(), style=p.foreground if code == 0 else p.error)
        self.query_one("#connect-result", Static).update(text)
        if code == 0:
            self.query_one("#yes", Button).label = "Done"
            self.query_one("#yes", Button).id = "done"

    @on(Button.Pressed, "#done")
    def done(self) -> None:
        self.dismiss(True)


# --------------------------------------------------------------------------------- app


class JezeroApp(App[None]):
    TITLE = "Jezero"
    CSS = CSS
    BINDINGS = [
        Binding("1", "show('home')", "Home"),
        Binding("2", "show('search')", "Search"),
        Binding("3", "show('timeline')", "Timeline"),
        Binding("4", "show('agents')", "Agents"),
        Binding("5", "show('context')", "Agent view"),
        Binding("slash", "find", "Find", show=False),
        Binding("escape", "blur", "Leave field", show=False),
        Binding("l", "newer", "Newer?", show=False),
        Binding("r", "reload", "Refresh"),
        Binding("t", "toggle_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, backend: Backend | None = None, demo: bool = False) -> None:
        super().__init__()
        self.demo = demo
        self.backend: Backend | None = backend
        self.problem: str | None = None
        if self.backend is None:
            try:
                self.backend = DemoBackend() if demo else RemoteBackend()
            except BackendError as exc:
                self.problem = str(exc)
        if self.backend is not None:
            # Every string from the server is shown without terminal control characters.
            self.backend = CleanBackend(self.backend)  # type: ignore[assignment]
        self.overview_data: dict[str, Any] = {}
        self.results: dict[str, dict] = {}
        self.current: dict[str, Any] | None = None
        self.current_newer: list[dict] | None = None
        self.current_matched_by = "subject"
        self.latest_items: list[dict] = []
        self.agent_statuses: list[AgentStatus] = []
        self.last_hits: tuple[str, list[dict]] | None = None
        self.timeline_state: tuple[str | None, list[dict]] | None = None

    @property
    def palette(self) -> Palette:
        return PALETTES.get(self.theme, PALETTES["jezero"])

    @property
    def now(self) -> datetime:
        backend = unwrap(self.backend)
        if isinstance(backend, DemoBackend):
            return backend.now
        return datetime.now(UTC)

    # ---------------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="brand")
            yield Tabs(
                Tab("Home", id="tab-home"),
                Tab("Search", id="tab-search"),
                Tab("Timeline", id="tab-timeline"),
                Tab("Agents", id="tab-agents"),
                Tab("Agent view", id="tab-context"),
                id="tabs",
            )
            yield Static(id="where")
        with ContentSwitcher(initial="home", id="views"):
            with VerticalScroll(id="home", classes="pane"):
                with Horizontal(id="hero"):
                    yield Static(id="wordmark")
                    with Grid(id="tiles"):
                        yield Tile("Memories", "t-items")
                        yield Tile("Last 14 days", "t-recent")
                        yield Tile("Sources", "t-sources")
                        yield Tile("Projects", "t-projects")
                with Horizontal(id="home-body"):
                    with Vertical(id="home-left"):
                        with Horizontal(id="home-charts"):
                            with Vertical(id="activity", classes="panel"):
                                yield Static(id="bars")
                            with Vertical(id="shares", classes="panel"):
                                yield Static(id="shares-body")
                        with VerticalScroll(id="sources-panel", classes="panel"):
                            yield Static(id="sources")
                    with Vertical(id="latest", classes="panel"):
                        yield OptionList(id="latest-list")
            with Vertical(id="search", classes="pane"):
                with Horizontal(id="search-bar"):
                    yield Static("❯", id="search-icon")
                    yield Input(
                        placeholder="Search like an agent: 2 to 5 distinctive words, "
                        "names, errors, ids",
                        id="query",
                    )
                with Horizontal(id="search-body"):
                    with Vertical(id="results", classes="panel"):
                        yield OptionList(id="results-list")
                    with VerticalScroll(id="detail", classes="panel"):
                        yield Static(id="detail-body")
            with Horizontal(id="timeline", classes="pane"):
                with Vertical(id="projects", classes="panel"):
                    yield OptionList(id="projects-list")
                with Vertical(id="timeline-panel", classes="panel"):
                    yield OptionList(id="timeline-list")
            with VerticalScroll(id="agents", classes="pane"):
                with Vertical(id="computer", classes="panel"):
                    yield Static(id="computer-body")
                yield Grid(id="agent-grid")
            with Vertical(id="context", classes="pane"):
                with Horizontal(id="ctx-bar"):
                    yield Select([], prompt="Project", id="ctx-project")
                    yield Input(
                        placeholder="A first message, as you would type it to an agent",
                        id="ctx-prompt",
                    )
                with Horizontal(id="ctx-body"):
                    with VerticalScroll(id="ctx-start", classes="panel"):
                        yield Static(classes="ctx-text", id="ctx-start-text")
                    with VerticalScroll(id="ctx-first", classes="panel"):
                        yield Static(classes="ctx-text", id="ctx-first-text")
        yield Footer()

    def on_mount(self) -> None:
        for t in THEMES:
            self.register_theme(t)
        self.theme = "jezero"
        self.query_one("#activity").border_title = "Activity · last 14 days"
        self.query_one("#shares").border_title = "Projects"
        self.query_one("#sources-panel").border_title = "Sources"
        self.query_one("#latest").border_title = "Latest memories"
        self.query_one("#results").border_title = "Results"
        self.query_one("#detail").border_title = "Memory"
        self.query_one("#projects").border_title = "Projects"
        self.query_one("#timeline-panel").border_title = "Timeline"
        self.query_one("#computer").border_title = "This computer"
        self.query_one("#ctx-start").border_title = "At session start"
        self.query_one("#ctx-first").border_title = "Before the first answer"
        self.paint()
        self.show_search_welcome()
        if self.problem:
            self.show_problem()
        else:
            self.load_overview()

    def watch_theme(self) -> None:
        if self.is_mounted:
            self.paint()

    def paint(self) -> None:
        """Parts drawn with Rich colours: redraw them when the theme changes."""
        p = self.palette
        brand = Text()
        brand.append("◭ ", style=f"bold {p.teal}")
        brand.append("jezero", style=f"bold {p.foreground}")
        self.query_one("#brand", Static).update(brand)
        self.query_one("#wordmark", Static).update(self.hero_text())
        self.paint_where()
        if self.overview_data:
            self.paint_overview()
            self.fill_latest(self.latest_items)
            self.fill_project_list(self.overview_data)
            self.fill_agents(self.agent_statuses)
        if self.last_hits:
            highlighted = self.query_one("#results-list", OptionList).highlighted
            self.show_results(*self.last_hits, focus=False)
            if highlighted is not None:
                self.query_one("#results-list", OptionList).highlighted = highlighted
        if self.current is not None:
            view = item_view(
                self.current, p, self.current_newer, matched_by=self.current_matched_by
            )
            self.query_one("#detail-body", Static).update(view)
        if self.timeline_state:
            self.show_timeline(*self.timeline_state)

    def hero_text(self) -> Text:
        p = self.palette
        text = wordmark(p)
        text.append("\n\n")
        text.append("  the shared memory of your agents", style=f"italic {p.muted}")
        return text

    def paint_where(self) -> None:
        p = self.palette
        where = Text(no_wrap=True)
        if self.problem:
            where.append("● ", style=p.error)
            where.append("not connected", style=p.muted)
        else:
            data = self.overview_data
            where.append("● ", style=p.success if data else p.faint)
            if data.get("device"):
                where.append(f"{data['device']}  ", style=p.foreground)
                where.append("uploads" if data.get("upload") else "read-only", style=p.muted)
                where.append("  ")
            where.append(self.backend.label if self.backend else "", style=p.muted)
        self.query_one("#where", Static).update(where)

    def show_problem(self) -> None:
        p = self.palette
        text = Text()
        text.append("Not connected yet\n\n", style=f"bold {p.foreground}")
        text.append(f"{self.problem}\n\n", style=p.muted)
        text.append("Press  c  to connect this computer, or run  ", style=p.muted)
        text.append("jezero ui --demo", style=p.teal)
        text.append("  to look around with made-up data.", style=p.muted)
        self.query_one("#bars", Static).update(text)
        self.paint_where()
        self.bind("c", "connect", description="Connect")

    def action_connect(self) -> None:
        def connected(ok: bool | None) -> None:
            if ok:
                self.notify("Key created. Once the admin approves it, press r.", timeout=8)

        self.push_screen(ConnectScreen(), connected)

    # -------------------------------------------------------------------- overview

    @work(thread=True, exclusive=True, group="overview")
    def load_overview(self) -> None:
        assert self.backend is not None
        try:
            data = self.backend.overview(14)
            latest = self.backend.timeline(None, 14)
            agents = self.backend.agents()
        except BackendError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)
            self.problem = str(exc)
            self.call_from_thread(self.show_problem)
            return
        self.problem = None
        self.call_from_thread(self.apply_overview, data, latest, agents)

    def apply_overview(
        self, data: dict[str, Any], latest: list[dict], agents: list[AgentStatus]
    ) -> None:
        self.overview_data = data
        self.latest_items = latest
        self.agent_statuses = agents
        self.paint()
        self.fill_projects(data)
        self.refresh_context()

    def paint_overview(self) -> None:
        p = self.palette
        data = self.overview_data
        sources: dict[str, Any] = data.get("sources") or {}
        days: list[str] = data.get("days") or []
        totals = [
            sum(int((s.get("daily") or [0] * len(days))[i]) for s in sources.values())
            for i in range(len(days))
        ]
        recent = sum(totals)
        today = totals[-1] if totals else 0
        live = sum(
            1
            for s in sources.values()
            if (parse_dt(s.get("latest")) or datetime.min.replace(tzinfo=UTC))
            > self.now - timedelta(days=1)
        )
        self.query_one("#t-items", Tile).set(_int(data.get("items")), f"+{today} today")
        self.query_one("#t-recent", Tile).set(_int(recent), f"~{recent // max(1, len(days))} a day")
        self.query_one("#t-sources", Tile).set(str(len(sources)), f"{live} active today")
        projects = data.get("projects") or {}
        top = next(iter(projects), None)
        self.query_one("#t-projects", Tile).set(str(len(projects)), f"latest: {top}" if top else "")
        if days:
            chart = bars(days, totals, p, height=7)
            best = max(range(len(totals)), key=lambda i: totals[i])
            caption = Text()
            caption.append(f"\n\n{_int(recent)} new memories", style=f"bold {p.foreground}")
            busiest = datetime.fromisoformat(days[best]).strftime("%a %d")
            caption.append(f" · busiest {busiest} ({totals[best]})", style=p.muted)
            chart.append_text(caption)
            self.query_one("#bars", Static).update(chart)
        self.query_one("#sources", Static).update(sources_table(sources, p, self.now))
        self.query_one("#shares-body", Static).update(project_shares(projects, p, self.now))

    def fill_latest(self, items: list[dict]) -> None:
        p = self.palette
        self.latest_items = items
        options = self.query_one("#latest-list", OptionList)
        options.clear_options()
        width = max(24, self.size.width * 2 // 5 - 10)
        for item in items:
            self.results[str(item["id"])] = item
            options.add_option(Option(result_line(item, p, width), id=f"i{item['id']}"))
            options.add_option(None)

    # ---------------------------------------------------------------------- search

    def show_search_welcome(self) -> None:
        p = self.palette
        text = Text()
        art = [
            "        ·  ·",
            "     ·  ╲  ╱  ·",
            "   ·  ── ◭ ──  ·",
            "     ·  ╱  ╲  ·",
            "        ·  ·",
        ]
        for line in art:
            text.append(line + "\n", style=p.teal)
        text.append("\nSearch the way your agents do\n\n", style=f"bold {p.foreground}")
        tips = [
            ("2 to 5 distinctive words", "names, ids, the exact error text"),
            ("matching is literal", "no luck? try a synonym"),
            ("enter", "opens the memory · l checks if something newer changed it"),
        ]
        for a, b in tips:
            text.append(f"  {a}", style=p.ochre)
            text.append(f"  {b}\n", style=p.muted)
        self.query_one("#detail-body", Static).update(text)

    def action_find(self) -> None:
        self.action_show("search")
        self.query_one("#query", Input).focus()

    @on(Input.Submitted, "#query")
    def run_search(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.search(event.value.strip())

    @work(thread=True, exclusive=True, group="search")
    def search(self, query: str) -> None:
        assert self.backend is not None
        self.call_from_thread(setattr, self.query_one("#results-list"), "loading", True)
        try:
            hits = self.backend.search(query, None, 25)
        except BackendError as exc:
            hits = []
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.show_results, query, hits)

    def show_results(self, query: str, hits: list[dict], focus: bool = True) -> None:
        p = self.palette
        self.last_hits = (query, hits)
        options = self.query_one("#results-list", OptionList)
        options.loading = False
        options.clear_options()
        self.query_one("#results").border_subtitle = f"{len(hits)} for “{query}”"
        if not hits:
            text = Text()
            text.append("\nNothing in Jezero for that.\n\n", style=f"bold {p.foreground}")
            text.append(
                "Matching is literal: try a synonym, a name, or fewer words.", style=p.muted
            )
            self.query_one("#detail-body", Static).update(text)
            return
        width = max(24, self.size.width * 2 // 5 - 10)
        for item in hits:
            self.results[str(item["id"])] = item
            options.add_option(Option(result_line(item, p, width), id=f"i{item['id']}"))
            options.add_option(None)
        if focus:
            options.highlighted = 0
            options.focus()

    @on(OptionList.OptionHighlighted, "#results-list")
    def preview(self, event: OptionList.OptionHighlighted) -> None:
        item = self.results.get(str(event.option.id)[1:])
        if item and item is not self.current:
            self.current = item
            self.current_newer = None
            self.current_matched_by = "subject"
            self.query_one("#detail-body", Static).update(item_view(item, self.palette))
            self.query_one("#detail").scroll_home(animate=False)

    @on(OptionList.OptionSelected, "#results-list")
    def open_result(self) -> None:
        self.query_one("#detail").focus()

    def action_newer(self) -> None:
        if self.query_one(ContentSwitcher).current == "search" and self.current:
            self.check_newer(self.current)

    @work(thread=True, exclusive=True, group="newer")
    def check_newer(self, item: dict[str, Any]) -> None:
        assert self.backend is not None
        try:
            newer, how = self.backend.later(int(item["id"]), None)
        except BackendError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return

        def show() -> None:
            if self.current is item:
                self.current_newer = newer
                self.current_matched_by = how
                view = item_view(item, self.palette, newer, matched_by=how)
                self.query_one("#detail-body", Static).update(view)

        self.call_from_thread(show)

    @on(OptionList.OptionSelected, "#latest-list, #timeline-list")
    def open_item(self, event: OptionList.OptionSelected) -> None:
        item = self.results.get(str(event.option.id)[1:])
        if item and self.backend:
            self.push_screen(ItemScreen(self.backend, item))

    # -------------------------------------------------------------------- timeline

    def fill_projects(self, data: dict[str, Any]) -> None:
        self.fill_project_list(data)
        self.load_timeline(None)
        select = self.query_one("#ctx-project", Select)
        select.set_options([(name, name) for name in (data.get("projects") or {})])

    def fill_project_list(self, data: dict[str, Any]) -> None:
        p = self.palette
        options = self.query_one("#projects-list", OptionList)
        keep = options.highlighted
        options.clear_options()
        options.add_option(
            Option(Text.assemble(("◭ ", p.teal), ("All projects", f"bold {p.foreground}")), id="p*")
        )
        for name, info in (data.get("projects") or {}).items():
            label = Text.assemble(("● ", project_color(name, p)), (name, p.foreground))
            label.append(f"  {_int(info.get('items'))}", style=p.muted)
            options.add_option(Option(label, id=f"p{name}"))
        with options.prevent(OptionList.OptionHighlighted):
            options.highlighted = keep or 0

    @on(OptionList.OptionHighlighted, "#projects-list")
    def pick_project(self, event: OptionList.OptionHighlighted) -> None:
        name = str(event.option.id)[1:]
        self.load_timeline(None if name == "*" else name)

    @work(thread=True, exclusive=True, group="timeline")
    def load_timeline(self, project: str | None) -> None:
        assert self.backend is not None
        try:
            items = self.backend.timeline(project, 50)
        except BackendError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self.show_timeline, project, items)

    def show_timeline(self, project: str | None, items: list[dict]) -> None:
        p = self.palette
        self.timeline_state = (project, items)
        options = self.query_one("#timeline-list", OptionList)
        options.clear_options()
        self.query_one("#timeline-panel").border_title = f"Timeline · {project or 'all projects'}"
        day = None
        for item in items:
            dt = parse_dt(item.get("occurred_at"))
            label = dt.astimezone().strftime("%A, %B %d") if dt else "undated"
            if label != day:
                day = label
                options.add_option(
                    Option(Text(f"\n{label.upper()}", style=f"bold {p.muted}"), disabled=True)
                )
            self.results[str(item["id"])] = item
            line = Text()
            line.append(dt.astimezone().strftime("%H:%M  ") if dt else "", style=p.muted)
            line.append_text(result_line(item, p, 90))
            options.add_option(Option(line, id=f"i{item['id']}"))

    # ---------------------------------------------------------------------- agents

    def fill_agents(self, agents: list[AgentStatus]) -> None:
        p = self.palette
        self.agent_statuses = agents
        grid = self.query_one("#agent-grid", Grid)
        cards = {c.status.agent: c for c in grid.query(AgentCard)}
        for status in agents:
            if status.agent in cards:
                cards[status.agent].refresh_card(status)
            else:
                grid.mount(AgentCard(status))
        data = self.overview_data
        text = Text()
        rows = [
            ("Server", self.backend.label if self.backend else "none"),
            ("This computer", str(data.get("device") or "unknown")),
            (
                "Mode",
                "uploads its sessions"
                if data.get("upload")
                else "read only: searches, never uploads",
            ),
            (
                "Key",
                f"{core.HOME}  (the key itself is never shown)" if not self.demo else "~/.jezero",
            ),
            ("Client", f"v{core.VERSION}"),
        ]
        pending = len(list((core.SPOOL / "pending").glob("*.json"))) if not self.demo else 0
        if data.get("upload"):
            rows.append(("Queued uploads", str(pending)))
        for k, v in rows:
            text.append(f"{k:<16}", style=p.muted)
            text.append(f"{v}\n", style=p.foreground)
        installed = sum(1 for a in agents if a.installed)
        text.append(
            f"\n{installed} of {len(agents)} agents use Jezero here", style=f"bold {p.teal}"
        )
        self.query_one("#computer-body", Static).update(text)

    @on(Button.Pressed, ".agent Button")
    def ask_install(self, event: Button.Pressed) -> None:
        agent = str(event.button.id).removeprefix("install-")
        card = self.query_one(f"#agent-{agent}", AgentCard)

        def go(ok: bool | None) -> None:
            if ok:
                if self.demo:
                    self.notify(f"Demo: {card.status.name} would be set up now.")
                else:
                    self.install(card.status)

        self.push_screen(ConfirmInstall(card.status), go)

    @work(thread=True, exclusive=True, group="install")
    def install(self, status: AgentStatus) -> None:
        self.call_from_thread(self.notify, f"Setting up Jezero in {status.name}…")
        ok, output = run_install(status)
        last = output.splitlines()[-1] if output else ""
        if ok:
            self.call_from_thread(self.notify, f"{status.name}: done. {last}", timeout=8)
        else:
            self.call_from_thread(
                self.notify, f"{status.name} failed: {last}", severity="error", timeout=12
            )
        self.load_overview()

    # ------------------------------------------------------------------ agent view

    @on(Select.Changed, "#ctx-project")
    @on(Input.Submitted, "#ctx-prompt")
    def refresh_context(self) -> None:
        if self.backend is None:
            return
        value = self.query_one("#ctx-project", Select).value
        project = value if isinstance(value, str) else None
        self.load_context(project, self.query_one("#ctx-prompt", Input).value)

    @work(thread=True, exclusive=True, group="context")
    def load_context(self, project: str | None, prompt: str) -> None:
        assert self.backend is not None
        try:
            ctx = self.backend.context(project, prompt)
        except BackendError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self.show_context, project, prompt, ctx)

    def show_context(self, project: str | None, prompt: str, ctx: dict[str, str]) -> None:
        p = self.palette
        start, first = ctx.get("session_start", ""), ctx.get("first_prompt", "")
        self.query_one("#ctx-start-text", Static).update(self.render_context(start))
        if first:
            self.query_one("#ctx-first-text", Static).update(self.render_context(first))
        else:
            hint = Text()
            if prompt.strip():
                hint.append(
                    "Nothing related enough: the agent gets no extra context.\n\n",
                    style=p.foreground,
                )
                hint.append(
                    "Jezero only adds past work when at least two words of the message ",
                    style=p.muted,
                )
                hint.append("appear in a memory's title or summary.", style=p.muted)
            else:
                hint.append(
                    "Type a first message above and press enter to see what Jezero ", style=p.muted
                )
                hint.append("adds before the agent answers.", style=p.muted)
            self.query_one("#ctx-first-text", Static).update(hint)
        self.query_one(
            "#ctx-start"
        ).border_subtitle = f"{len(start):,} chars · ~{len(start) // 4:,} tokens"
        self.query_one(
            "#ctx-first"
        ).border_subtitle = f"{len(first):,} chars · ~{len(first) // 4:,} tokens"
        if project is None:
            self.query_one(
                "#ctx-start"
            ).border_title = "At session start · pick a project for recent work"
        else:
            self.query_one("#ctx-start").border_title = f"At session start · {project}"

    def render_context(self, raw: str) -> Text:
        p = self.palette
        out = Text()
        for line in raw.splitlines():
            if line.startswith("## "):
                out.append(line[3:] + "\n", style=f"bold {p.ochre}")
            elif line.startswith("- ["):
                ident, _, rest = line[2:].partition("] ")
                out.append("• ", style=p.teal)
                out.append(ident + "] ", style=p.sand)
                date, _, title = rest.partition(" ")
                out.append(date + " ", style=p.muted)
                out.append(title + "\n", style=p.foreground)
            elif line.startswith("- "):
                out.append("• ", style=p.teal)
                out.append_text(inline_code(line[2:] + "\n", p.foreground, p.teal))
            elif line.strip():
                base = f"bold {p.foreground}" if line.endswith(":") else p.foreground
                out.append_text(inline_code(line + "\n", base, p.teal))
            else:
                out.append("\n")
        return out

    # ------------------------------------------------------------------- actions

    @on(Tabs.TabActivated, "#tabs")
    def switch(self, event: Tabs.TabActivated) -> None:
        self.query_one(ContentSwitcher).current = str(event.tab.id).removeprefix("tab-")

    def action_show(self, view: str) -> None:
        self.query_one("#tabs", Tabs).active = f"tab-{view}"
        self.query_one(ContentSwitcher).current = view
        if view == "search":
            self.query_one("#query", Input).focus()

    def action_reload(self) -> None:
        if self.problem and not self.demo:
            try:
                self.backend = RemoteBackend()
                self.problem = None
            except BackendError as exc:
                self.problem = str(exc)
                self.show_problem()
                return
        self.load_overview()

    def action_blur(self) -> None:
        self.screen.set_focus(None)

    def action_toggle_theme(self) -> None:
        self.theme = "jezero-dawn" if self.theme == "jezero" else "jezero"


def run(demo: bool = False) -> int:
    JezeroApp(demo=demo).run()
    return 0


__all__ = ["AGENT_NAMES", "JezeroApp", "run"]
