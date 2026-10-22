"""Build the documentation website from docs/.

    uv run --group docs python scripts/build_site.py           # build into build/site
    uv run --group docs python scripts/build_site.py --serve   # preview with live reload

docs/ is written for GitHub first, so the site is built from a staged copy in
build/site-src:

- docs/README.md becomes the home page, with a hero and four cards on top;
- links that leave docs/ (CONTRIBUTING.md, the example config...) point to the file on
  GitHub, and links to a folder's README.md point to its index page;
- website/assets (logo, colours) and the CNAME file of the custom domain are added.

Configuration: zensical.toml. Deployed by .github/workflows/docs.yml on every push to main.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STAGE = ROOT / "build" / "site-src"
ASSETS = ROOT / "website" / "assets"
REPO = "https://github.com/MarsGotta/jezero"
DOMAIN = "jezero.marsgotta.com"
"""The site's custom domain, written to CNAME for GitHub Pages."""

SKIP = {"architecture.md"}
"""Pages left out of the site: the old architecture path is a pointer kept for GitHub."""

LINK = re.compile(r"(\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))|(src=\")([^\"]+)(\")")
FENCE = re.compile(r"^\s*(```|~~~)")

HERO = """\
<div class="jz-hero" markdown>
<div markdown>

# Jezero

<p class="jz-tagline">A shared, searchable library of your agents' past work. Every
session of every agent, on every computer, summarised once and served read-only over MCP.
</p>

[Your first library](getting-started/tutorial.md){ .md-button .md-button--primary }
[Connect an agent](how-to/connect-agents.md){ .md-button }

</div>

![The Jezero app: counters, a 14-day activity chart, projects, sources and the latest
items.](app/home.svg)

</div>

<div class="grid cards" markdown>

-   :material-school-outline: **Learn**

    ---

    From nothing to a working library in about fifteen minutes, then a second computer.

    [Start the tutorial →](getting-started/tutorial.md)

-   :material-wrench-outline: **Do**

    ---

    Run the server, add sources, connect agents and computers, fix what breaks.

    [Run Jezero as a service →](how-to/deploy.md)

-   :material-book-open-variant: **Look up**

    ---

    Every command, configuration key, connector, MCP tool and route. Exact and complete.

    [Command reference →](reference/cli.md)

-   :material-lightbulb-outline: **Understand**

    ---

    How the parts fit, how search ranks, how summaries are made, what is protected.

    [Architecture →](explanation/architecture.md)

</div>
"""


def rewrite(target: str, source: Path) -> str:
    """The link `target`, written in docs/`source`, as the site needs it."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
        return target
    path, _, anchor = target.partition("#")
    if not path:
        return target
    here = posixpath.dirname(source.as_posix())
    resolved = posixpath.normpath(posixpath.join(here, path))
    if resolved.startswith("../") or resolved == "..":
        repo_path = posixpath.normpath(posixpath.join("docs", here, path))
        kind = "tree" if (ROOT / repo_path).is_dir() else "blob"
        url = f"{REPO}/{kind}/main/{repo_path}"
        return f"{url}#{anchor}" if anchor else url
    if posixpath.basename(path) == "README.md":
        path = posixpath.join(posixpath.dirname(path), "index.md")
    return f"{path}#{anchor}" if anchor else path


def stage_page(text: str, source: Path) -> str:
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        if not fenced:

            def fix(m: re.Match[str]) -> str:
                if m.group(1):
                    return m.group(1) + rewrite(m.group(2), source) + m.group(3)
                return m.group(4) + rewrite(m.group(5), source) + m.group(6)

            line = LINK.sub(fix, line)
        out.append(line)
    return "\n".join(out) + "\n"


def home_page(text: str) -> str:
    """docs/README.md with the hero in place of its title and opening paragraphs."""
    body = text.split("\n## ", 1)[1]
    front = "---\ntitle: Shared memory for your agents\nhide:\n  - navigation\n  - toc\n---\n"
    return front + "\n" + HERO + "\n## " + body


def stage() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    for src in sorted(DOCS.rglob("*")):
        rel = src.relative_to(DOCS)
        if src.is_dir() or rel.as_posix() in SKIP:
            continue
        dest = STAGE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix != ".md":
            shutil.copy2(src, dest)
            continue
        text = stage_page(src.read_text(encoding="utf-8"), rel)
        if rel.name == "README.md":
            dest = dest.with_name("index.md")
            if rel.parent == Path("."):
                text = home_page(text)
        dest.write_text(text, encoding="utf-8")
    shutil.copytree(ASSETS, STAGE / "assets")
    (STAGE / "CNAME").write_text(DOMAIN + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the documentation website from docs/.")
    parser.add_argument("--serve", action="store_true", help="preview with live reload")
    parser.add_argument("--stage-only", action="store_true", help="only write build/site-src")
    args = parser.parse_args(argv)
    stage()
    if args.stage_only:
        print(f"staged {STAGE.relative_to(ROOT)}")
        return 0
    command = ["zensical", "serve"] if args.serve else ["zensical", "build", "--clean", "--strict"]
    return subprocess.run([*command, "-f", str(ROOT / "zensical.toml")], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
