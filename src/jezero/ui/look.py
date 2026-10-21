"""Looks of the `jezero` app: two themes, the wordmark and small Rich helpers.

Jezero is a crater on Mars where a river once fanned out into a delta. The dark theme is
the crater at night (rust and ochre on warm black) with the delta's water as the second
colour; the light theme is the same place at dawn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rich.text import Text
from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    background: str
    surface: str
    panel: str
    foreground: str
    muted: str
    faint: str
    rust: str
    ochre: str
    sand: str
    teal: str
    success: str
    warning: str
    error: str
    dark: bool


NIGHT = Palette(
    background="#120D0B",
    surface="#19120F",
    panel="#221813",
    foreground="#EFE3D3",
    muted="#9C8B7C",
    faint="#4A3B32",
    rust="#D9572B",
    ochre="#EE8A45",
    sand="#F2C46D",
    teal="#3FC9B7",
    success="#7BD389",
    warning="#F2C46D",
    error="#F2665C",
    dark=True,
)
DAWN = Palette(
    background="#F7F0E6",
    surface="#F0E6D8",
    panel="#E7D8C5",
    foreground="#2B1D16",
    muted="#7A6556",
    faint="#CDBBA6",
    rust="#B9461B",
    ochre="#C9662A",
    sand="#9C6F12",
    teal="#127E71",
    success="#2E7D45",
    warning="#9C6F12",
    error="#B3261E",
    dark=False,
)
PALETTES = {"jezero": NIGHT, "jezero-dawn": DAWN}


def theme(name: str, p: Palette) -> Theme:
    return Theme(
        name=name,
        primary=p.ochre,
        secondary=p.teal,
        accent=p.sand,
        warning=p.warning,
        error=p.error,
        success=p.success,
        foreground=p.foreground,
        background=p.background,
        surface=p.surface,
        panel=p.panel,
        dark=p.dark,
        variables={
            "text-muted": p.muted,
            "border": p.faint,
            "border-blurred": p.faint,
            "footer-key-foreground": p.ochre,
            "footer-description-foreground": p.muted,
            "input-selection-background": f"{p.teal} 35%",
            "block-cursor-background": p.panel,
            "block-cursor-foreground": p.foreground,
            "block-cursor-text-style": "bold",
            "scrollbar": p.faint,
            "scrollbar-hover": p.muted,
            "scrollbar-active": p.ochre,
            "scrollbar-background": p.background,
        },
    )


THEMES = [theme(name, p) for name, p in PALETTES.items()]

WORDMARK = """\
     ██╗███████╗███████╗███████╗██████╗  ██████╗
     ██║██╔════╝╚══███╔╝██╔════╝██╔══██╗██╔═══██╗
     ██║█████╗    ███╔╝ █████╗  ██████╔╝██║   ██║
██   ██║██╔══╝   ███╔╝  ██╔══╝  ██╔══██╗██║   ██║
╚█████╔╝███████╗███████╗███████╗██║  ██║╚██████╔╝
 ╚════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝"""


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def blend(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb, strict=True)))  # type: ignore[arg-type]


def gradient(stops: list[str], t: float) -> str:
    """Colour at `t` (0..1) along evenly spaced stops."""
    t = max(0.0, min(1.0, t))
    if len(stops) == 1:
        return stops[0]
    scaled = t * (len(stops) - 1)
    i = min(int(scaled), len(stops) - 2)
    return blend(stops[i], stops[i + 1], scaled - i)


def wordmark(p: Palette) -> Text:
    """The name in block letters: rust to sand to delta water, with a darker shadow."""
    lines = WORDMARK.splitlines()
    width = max(len(line) for line in lines)
    stops = [p.rust, p.ochre, p.sand, p.teal]
    out = Text(no_wrap=True)
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            if ch == " ":
                out.append(" ")
                continue
            color = gradient(stops, col / max(1, width - 1))
            if ch != "█":  # the box-drawing shadow
                color = blend(color, p.background, 0.55)
            out.append(ch, style=f"bold {color}" if ch == "█" else color)
        if row < len(lines) - 1:
            out.append("\n")
    return out


BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[int], p: Palette, width: int | None = None) -> Text:
    values = values[-width:] if width else values
    top = max(values) if values and max(values) > 0 else 1
    out = Text(no_wrap=True)
    for v in values:
        level = 0 if v <= 0 else max(1, round(v / top * 8))
        color = gradient([p.rust, p.ochre, p.sand], level / 8) if v > 0 else p.faint
        out.append(BLOCKS[level] if v > 0 else "·", style=color)
    return out


def project_color(name: str, p: Palette) -> str:
    """A stable colour per project, picked from the theme's warm and water tones."""
    tones = [
        p.ochre,
        p.teal,
        p.sand,
        p.rust,
        blend(p.teal, p.sand, 0.5),
        blend(p.rust, p.sand, 0.5),
    ]
    digest = hashlib.sha256(name.encode()).digest()[0]
    return tones[digest % len(tones)]


def chip(label: str, color: str, p: Palette) -> Text:
    """A small filled label, like `atlas` or `#2412`."""
    return Text(f" {label} ", style=f"bold {p.background} on {color}")


ATOMS = {
    "decision": ("◆", "Decision"),
    "fix": ("✔", "Fix"),
    "error": ("✖", "Error"),
    "command": ("❯", "Command"),
    "fact": ("•", "Fact"),
    "rationale": ("∵", "Why"),
    "risk": ("▲", "Risk"),
}


def atom_color(kind: str, p: Palette) -> str:
    return {
        "decision": p.ochre,
        "fix": p.success,
        "error": p.error,
        "command": p.teal,
        "fact": p.foreground,
        "rationale": p.muted,
        "risk": p.warning,
    }.get(kind, p.foreground)


AGENT_GLYPHS = {
    "claude-code": "✻",
    "codex": "◎",
    "opencode": "⌬",
    "hermes": "☿",
    "antigravity": "△",
}
