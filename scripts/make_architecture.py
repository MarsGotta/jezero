"""Draw docs/architecture.svg, the diagram at the top of the README.

    python3 scripts/make_architecture.py

Plain SVG with a white background, so it reads the same in light and dark mode on
GitHub. Edit the boxes below and run it again; nothing else depends on it.
"""

from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"

PALETTE = {
    "src": ("#e8f1ff", "#2f62c9"),
    "dev": ("#fff4e0", "#b8730f"),
    "srv": ("#eef8ef", "#237a3a"),
    "lib": ("#e6f6f4", "#0e7268"),
    "use": ("#f4ecff", "#6a3ec0"),
    "ops": ("#fdecec", "#b53a3a"),
    "core": ("#f3f3f3", "#555555"),
}
GREY = "#555555"
parts: list[str] = []


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def text(x, y, value, color="#333", size: float = 12, weight=400, anchor="start"):
    parts.append(
        f'<text xml:space="preserve" x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
        f'text-anchor="{anchor}">{esc(value)}</text>'
    )


def box(x, y, w, h, kind, title, lines=(), dashed=False, title_size=14):
    fill, stroke = PALETTE[kind]
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.6"{dash}/>'
    )
    text(x + 12, y + 22, title, stroke, title_size, 700)
    for i, line in enumerate(lines):
        text(x + 12, y + 42 + i * 17, line)


def group(x, y, w, h, kind, title):
    _, stroke = PALETTE[kind]
    parts.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="none" '
        f'stroke="{stroke}" stroke-width="2"/>'
    )
    parts.append(
        f'<rect x="{x + 14}" y="{y - 11}" width="{len(title) * 7.6 + 18}" height="22" rx="6" '
        f'fill="{stroke}"/>'
    )
    text(x + 23, y + 5, title, "#ffffff", 13, 700)


def arrow(x1, y1, x2, y2, color=GREY, dashed=False, curve=False):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    if curve:
        mid = (x1 + x2) / 2
        path = f"M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}"
    else:
        path = f"M{x1},{y1} L{x2},{y2}"
    parts.append(
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"{dash} '
        f'marker-end="url(#m{color[1:]})"/>'
    )


def color(kind: str) -> str:
    return PALETTE[kind][1]


W, H = 1400, 930

# Arrows first, so the boxes draw over their ends.
arrow(335, 250, 425, 250, color("src"))
text(342, 240, "reads its", color("src"), 11.5)
text(342, 268, "files", color("src"), 11.5)
arrow(335, 610, 425, 610, color("dev"))
text(340, 598, "HTTPS", color("dev"), 11.5, 700)
text(340, 630, "on exit", color("dev"), 11.5)
arrow(545, 560, 545, 418, color("srv"))
text(553, 495, "same connectors", color("srv"), 11.5)
parts.append(
    f'<path d="M410,712 L410,380 L421,380" fill="none" stroke="{color("ops")}" '
    f'stroke-width="2" stroke-dasharray="5 4" marker-end="url(#m{color("ops")[1:]})"/>'
)
arrow(668, 160, 716, 160, color("srv"))
arrow(850, 196, 850, 222, color("srv"))
arrow(850, 316, 850, 340, color("lib"))
arrow(850, 456, 850, 500, color("lib"))
arrow(988, 560, 1068, 300, color("use"), curve=True)
arrow(988, 590, 1068, 600, color("use"), curve=True)
arrow(988, 700, 1068, 790, color("ops"), curve=True)
arrow(180, 718, 180, 800, color("core"), dashed=True)
text(190, 765, "they run the plugin", color("core"), 11.5)

text(30, 40, "1 · WHERE SESSIONS COME FROM", "#222", 13, 800)
text(400, 40, "2 · THE JEZERO SERVER", "#222", 13, 800)
text(1068, 40, "3 · WHO USES IT", "#222", 13, 800)

group(20, 68, 315, 380, "src", "On the server's computer")
for i, (name, how) in enumerate(
    [
        ("Hermes Agent", "conversations"),
        ("Claude Code", "~/.claude sessions"),
        ("Codex", "~/.codex sessions"),
        ("Codex per agent", "one home per agent"),
        ("OpenCode", "its database"),
        ("Antigravity", "its local store"),
        ("Paperclip", "tasks, via its API"),
    ]
):
    y = 88 + i * 50
    box(34, y, 287, 44, "src", name, title_size=13)
    text(311, y + 27, how, "#444", 11.5, anchor="end")

group(20, 490, 315, 228, "dev", "Other computers")
box(
    34,
    510,
    287,
    74,
    "dev",
    "Laptops and desktops",
    ("Claude Code, Codex, OpenCode", "with the Jezero plugin"),
)
box(34, 594, 287, 50, "dev", "Email", ("planned · stored as external",), dashed=True)
box(34, 654, 287, 50, "dev", "Antigravity, Cursor", ("planned · a folder watcher",), dashed=True)

group(392, 68, 610, 680, "srv", "One process · behind your VPN or a reverse proxy")
box(
    425,
    110,
    243,
    308,
    "srv",
    "Connectors",
    (
        "One per source type.",
        "Read only what is new since",
        "the last run (a cursor).",
        "Skip tool noise and hidden",
        "reasoning.",
        "Wait until a session is quiet.",
        "Project: configured folders,",
        "then the git repository, then",
        "folders like projects or code.",
        "Flag a format they do not",
        "recognise.",
    ),
)
box(
    425,
    560,
    243,
    120,
    "srv",
    "Inbox · /inbox/v1",
    (
        "Upload only, nothing to read.",
        "One key per device.",
        "Each device has its folder,",
        "plus the project it detected.",
    ),
)
box(400, 712, 230, 30, "ops", "systemd timer · every 5 min", title_size=12)
box(
    720,
    110,
    268,
    86,
    "srv",
    "Redaction",
    ("Tokens and passwords removed", "BEFORE summarising or storing."),
)
box(
    720,
    226,
    268,
    90,
    "srv",
    "Summariser",
    ("Any OpenAI- or Anthropic-", "compatible model: summary + atoms."),
)
box(
    720,
    344,
    268,
    112,
    "lib",
    "Library · SQLite",
    (
        "One file. FTS5 full-text index,",
        "BM25 with a recency boost.",
        "Atoms: decision, fix, error,",
        "command, fact. Real dates.",
    ),
)
box(
    720,
    504,
    268,
    140,
    "use",
    "MCP server · read-only",
    (
        "search · get_item · later",
        "timeline · list_sources",
        "A key per client; each sees",
        "only its allowed sources.",
        "later: did this change since?",
    ),
)
box(720, 668, 268, 64, "ops", "Watchdog · jezero health", ("Is a connector failing or", "stuck?"))

box(
    1068,
    80,
    312,
    300,
    "use",
    "Agents that ask",
    (
        "Any MCP client: Claude Code,",
        "Codex, OpenCode, Hermes...",
        "",
        "With the protocol they use it",
        "without being asked:",
        "· search before real work",
        "· check later before relying",
        "  on a decision",
        "· cite the item, like [123]",
        "· say so when nothing is found",
    ),
)
box(
    1068,
    410,
    312,
    330,
    "use",
    "The plugin in each agent",
    (
        "Session start:",
        "· the protocol and the project's",
        "  recent work",
        "First prompt:",
        "· related past work, only when",
        "  it really matches",
        "Session end:",
        "· queue the transcript, upload",
        "  in the background",
        "· offline? retried later",
        "",
        "One skill: how to search well.",
        "Registers the MCP server too.",
    ),
)
box(
    1068,
    765,
    312,
    70,
    "ops",
    "Uptime monitor",
    ("A push monitor: silence or", "failure becomes an alert."),
)

box(
    20,
    800,
    982,
    112,
    "core",
    "One shared core for every plugin",
    (
        "jezero_client.py: hooks, upload queue, device key, MCP setup. Python 3.9+, standard library only.",
        "jezero_projects.py: how a folder becomes a project. The SAME file the server uses.",
        "Thin adapters: Claude Code and Codex share their hooks · OpenCode gets a small TypeScript plugin.",
    ),
)
text(1068, 870, "dashed arrows: periodic", "#666", 11)
text(1068, 888, "dashed boxes: planned", "#666", 11)

markers = "".join(
    f'<marker id="m{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
    f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/>'
    "</marker>"
    for c in sorted({stroke for _, stroke in PALETTE.values()} | {GREY})
)
svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif">'
    f'<defs>{markers}</defs><rect width="{W}" height="{H}" rx="16" fill="#ffffff"/>'
    + "".join(parts)
    + "</svg>\n"
)
OUT.write_text(svg, encoding="utf-8")
print(f"wrote {OUT}")
