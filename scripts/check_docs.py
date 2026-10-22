"""Check the documentation: every relative link and anchor resolves, and no em dashes.

    uv run python scripts/check_docs.py

Covers every Markdown file of the repository except generated and vendored folders.
External links (http, mailto) are not fetched.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {
    ".git",
    ".venv",
    "node_modules",
    "plugins",
    "dist",
    "build",
    ".pytest_cache",
    "data",
    ".ruff_cache",
}
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<img[^>]+src=\"([^\"]+)\"")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def markdown_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md") if not any(part in SKIP for part in p.relative_to(ROOT).parts)
    )


def strip_code(text: str) -> str:
    """The text outside fenced code blocks and inline code."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def anchor(heading: str) -> str:
    """GitHub's slug: lowercase, markup dropped, spaces to hyphens, punctuation removed."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").lower().strip()
    text = "".join(c for c in text if c.isalnum() or c in " -_")
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            slug = anchor(match.group(2))
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            found.add(slug if n == 0 else f"{slug}-{n}")
    found.update(re.findall(r'<a (?:name|id)="([^"]+)"', path.read_text(encoding="utf-8")))
    return found


def check() -> list[str]:
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}
    for md in markdown_files():
        rel = md.relative_to(ROOT)
        raw = md.read_text(encoding="utf-8")
        if rel.parts[0] == "docs" and "\u2014" in strip_code(raw):
            problems.append(f"{rel}: em dash (use a colon, a comma or a new sentence)")
        text = strip_code(raw)
        targets = [a or b for a, b in LINK.findall(text)] + IMAGE.findall(text)
        for target in targets:
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):  # http:, https:, mailto:
                continue
            path_part, _, frag = target.partition("#")
            dest = md if not path_part else (md.parent / path_part).resolve()
            if not dest.exists():
                problems.append(f"{rel}: broken link {target}")
                continue
            if frag and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors(dest)
                if frag not in cache[dest]:
                    problems.append(f"{rel}: no heading for #{frag} in {dest.relative_to(ROOT)}")
    return problems


def main() -> int:
    problems = check()
    for p in problems:
        print(p)
    count = len(markdown_files())
    print(
        f"{count} Markdown files, {len(problems)} problems"
        if problems
        else f"docs OK ({count} files)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
