#!/usr/bin/env python3
"""Assemble the Jezero client bundle from one source of truth.

    clients/core/jezero_client.py   shared core (hooks, upload, setup)
    clients/skill/SKILL.md          instructions for agents, shared by every plugin
    clients/claude-code/            Claude Code plugin manifest, hooks and MCP bridge (.mcp.json)
    clients/codex/                  Codex plugin manifest and hooks
    clients/opencode/               OpenCode plugin (jezero.ts) and its package.json
    clients/hermes/                 Hermes Agent plugin (plugin.yaml, __init__.py)
    clients/antigravity/            Antigravity plugin (plugin.json, hooks.json, mcp_config.json)

Output:

- dist/client/ (a plugin marketplace for Claude Code and Codex, plus the OpenCode and
  Hermes plugins and the core) and dist/jezero-client.tar.gz, which `jezero serve` offers at
  /client/jezero-client.tar.gz;
- plugins/ plus .claude-plugin/ and .agents/plugins/ at the repository root, committed,
  so `claude plugin marketplace add OWNER/jezero` and `codex plugin marketplace add
  OWNER/jezero` install straight from the repository.

Only files that git tracks are copied: an untracked or ignored file under clients/ (a
`.env`, `node_modules/`, a `*.bak`) stops the build instead of reaching the bundle. Outside
a git checkout (an sdist) the list is CLIENT_FILES, which must equal `git ls-files clients/`.
The bundle is reproducible (sorted entries, fixed owner and mtime from SOURCE_DATE_EPOCH or
the last commit), and its SHA-256 is printed so it can be compared with the release's
SHA256SUMS.

Run it after changing anything under clients/. `--check` only verifies that the
committed copy matches clients/ (CI runs it).
"""

from __future__ import annotations

import filecmp
import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clients"
DIST = ROOT / "dist"
OUT = DIST / "client"
PROJECTS = ROOT / "src" / "jezero" / "projects.py"
"""Project detection, shared with the server: shipped next to the core."""

CLIENT_FILES = (
    "clients/README.md",
    "clients/antigravity/hooks.json",
    "clients/antigravity/mcp_config.json",
    "clients/antigravity/plugin.json",
    "clients/claude-code/.claude-plugin/plugin.json",
    "clients/claude-code/.mcp.json",
    "clients/claude-code/hooks/hooks.json",
    "clients/codex/.codex-plugin/plugin.json",
    "clients/codex/hooks/hooks.json",
    "clients/core/jezero_client.py",
    "clients/hermes/__init__.py",
    "clients/hermes/plugin.yaml",
    "clients/opencode/jezero.ts",
    "clients/opencode/package.json",
    "clients/ruff.toml",
    "clients/skill/SKILL.md",
)
"""Every file under clients/. In a git checkout `git ls-files clients/` must list exactly
these (add a new file here when you add it to git); outside one (an sdist) only these are
read. Nothing else under clients/ is ever copied into the bundle or plugins/."""


CACHE_DIRS = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
"""Caches that tools write by themselves (`ruff check clients` makes clients/.ruff_cache,
because clients/ruff.toml is a ruff project root). Never copied, and allowed to exist."""


def _ignorable(rel: str) -> bool:
    return bool(CACHE_DIRS & set(rel.split("/"))) or rel.endswith((".pyc", ".pyo"))


def _git_files(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", *args, "--", "clients/"],
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p)


@functools.cache
def client_files() -> tuple[str, ...]:
    """The files of clients/ that may be shipped, relative to the repository root.

    Exits with a message when clients/ holds a file git does not track (or, outside a git
    checkout, a file not in CLIENT_FILES), or when a listed file is missing.
    """
    if (ROOT / ".git").exists() and shutil.which("git"):
        stray = [p for p in _git_files("--others") if not _ignorable(p)]
        if stray:
            raise SystemExit(
                "clients/ holds files that git does not track. They are never shipped, but\n"
                "the bundle must match the repository: delete them, move them out of\n"
                "clients/, or `git add` them (and list them in CLIENT_FILES):\n"
                + "\n".join(f"  {p}" for p in stray)
            )
        files = _git_files()
        if set(files) != set(CLIENT_FILES):
            missing = sorted(set(files) - set(CLIENT_FILES))
            extra = sorted(set(CLIENT_FILES) - set(files))
            raise SystemExit(
                "CLIENT_FILES in scripts/build_clients.py does not match `git ls-files clients/`"
                + "".join(f"\n  not in CLIENT_FILES: {p}" for p in missing)
                + "".join(f"\n  not tracked by git: {p}" for p in extra)
            )
    else:
        files = sorted(CLIENT_FILES)
        on_disk = sorted(
            p.relative_to(ROOT).as_posix()
            for p in SRC.rglob("*")
            if (p.is_file() or p.is_symlink()) and not _ignorable(p.relative_to(ROOT).as_posix())
        )
        stray = sorted(set(on_disk) - set(files))
        if stray:
            raise SystemExit(
                "clients/ holds files that are not in CLIENT_FILES; delete or move them:\n"
                + "\n".join(f"  {p}" for p in stray)
            )
    for rel in files:
        path = ROOT / rel
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"{rel} is missing or not a regular file")
    return tuple(files)


def copy_tree(agent: str, target: Path) -> None:
    """Copy clients/<agent>/ into `target`, file by file from client_files()."""
    prefix = f"clients/{agent}/"
    files = [rel for rel in client_files() if rel.startswith(prefix)]
    if not files:
        raise SystemExit(f"no files for {agent} under clients/")
    for rel in files:
        dest = target / rel[len(prefix) :]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)


def source_date_epoch() -> int:
    """The timestamp stored in the bundle: SOURCE_DATE_EPOCH, else the last commit's."""
    if os.environ.get("SOURCE_DATE_EPOCH", "").isdigit():
        return int(os.environ["SOURCE_DATE_EPOCH"])
    if (ROOT / ".git").exists() and shutil.which("git"):
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], capture_output=True, text=True
        ).stdout.strip()
        if out.isdigit():
            return int(out)
    return 315532800  # 1980-01-01, the oldest time a zip can hold


def write_bundle(folder: Path, bundle: Path, arcname: str = "jezero-client") -> str:
    """Write `folder` as a reproducible .tar.gz and return its SHA-256."""
    mtime = source_date_epoch()
    paths = [folder, *sorted(folder.rglob("*"), key=lambda p: p.relative_to(folder).as_posix())]

    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = mtime
        info.mode = 0o755 if info.isdir() or info.mode & 0o100 else 0o644
        return info

    with (
        bundle.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=mtime) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for path in paths:
            rel = path.relative_to(folder).as_posix()
            name = arcname if rel == "." else f"{arcname}/{rel}"
            tar.add(path, arcname=name, recursive=False, filter=normalize)
    return hashlib.sha256(bundle.read_bytes()).hexdigest()


def version() -> str:
    for line in (SRC / "core" / "jezero_client.py").read_text().splitlines():
        if line.startswith("VERSION = "):
            return line.split('"')[1]
    raise SystemExit("VERSION not found in the core")


DESCRIPTION = "Shared read-only memory of past agent work."
REPO_PLUGINS = "plugins"
"""Folder for the committed plugins, at the repository root."""
REPO_FILES = (
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    "src/jezero/client_core.py",
)
"""`src/jezero/client_core.py` is the core inside the package, for the `jezero` app."""


def plugin(agent: str, target: Path) -> None:
    copy_tree(agent, target)
    (target / "core").mkdir()
    shutil.copy2(SRC / "core" / "jezero_client.py", target / "core" / "jezero_client.py")
    shutil.copy2(PROJECTS, target / "core" / "jezero_projects.py")
    (target / "skills" / "jezero").mkdir(parents=True)
    shutil.copy2(SRC / "skill" / "SKILL.md", target / "skills" / "jezero" / "SKILL.md")
    for manifest in [*target.glob(".*-plugin/plugin.json"), *target.glob("package.json")]:
        data = json.loads(manifest.read_text())
        data["version"] = version()
        manifest.write_text(json.dumps(data, indent=2) + "\n")
    for manifest in target.glob("plugin.yaml"):
        text = re.sub(r"(?m)^version: .*$", f"version: {version()}", manifest.read_text())
        manifest.write_text(text)


def marketplaces(root: Path, prefix: str) -> None:
    """Write the Claude Code and Codex marketplace files under `root`."""
    claude_market = root / ".claude-plugin" / "marketplace.json"
    claude_market.parent.mkdir(parents=True, exist_ok=True)
    claude_market.write_text(
        json.dumps(
            {
                "name": "jezero",
                "owner": {"name": "Jezero"},
                "metadata": {"description": DESCRIPTION},
                "plugins": [
                    {
                        "name": "jezero",
                        "source": f"./{prefix}claude-code",
                        "description": DESCRIPTION,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
    codex_market = root / ".agents" / "plugins" / "marketplace.json"
    codex_market.parent.mkdir(parents=True, exist_ok=True)
    codex_market.write_text(
        json.dumps(
            {
                "name": "jezero",
                "interface": {"displayName": "Jezero"},
                "plugins": [
                    {
                        "name": "jezero",
                        "source": {"source": "local", "path": f"./{prefix}codex"},
                        "policy": {"installation": "AVAILABLE"},
                        "category": "Productivity",
                        "interface": {"displayName": "Jezero"},
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )


def plugins(base: Path) -> None:
    for agent in ("claude-code", "codex", "opencode", "hermes", "antigravity"):
        plugin(agent, base / agent)


GENERATED_NOTE = """\
# Generated: do not edit

Installable copy of the Jezero plugins, so Claude Code and Codex can install them
straight from this repository. `scripts/build_clients.py` writes it from `clients/`,
which is the only place to change them; CI fails if this folder differs.

How to install and connect a computer:
[Connect an agent](https://github.com/MarsGotta/jezero/blob/main/docs/how-to/connect-agents.md).
"""


def repo_copy(root: Path) -> None:
    """The committed copy: plugins/ (with a do-not-edit note) and the marketplace files."""
    plugins(root / REPO_PLUGINS)
    (root / REPO_PLUGINS / "README.md").write_text(GENERATED_NOTE)
    marketplaces(root, f"{REPO_PLUGINS}/")
    (root / "src" / "jezero").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC / "core" / "jezero_client.py", root / "src" / "jezero" / "client_core.py")


def same_tree(a: Path, b: Path) -> list[str]:
    cmp = filecmp.dircmp(a, b)
    diffs = [str(b / n) for n in cmp.left_only + cmp.right_only + cmp.funny_files]
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    diffs += [str(b / n) for n in mismatch + errors]
    for sub in cmp.common_dirs:
        diffs += same_tree(a / sub, b / sub)
    return diffs


def check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        repo_copy(fresh)
        diffs: list[str] = []
        if (ROOT / REPO_PLUGINS).is_dir():
            diffs += same_tree(fresh / REPO_PLUGINS, ROOT / REPO_PLUGINS)
        else:
            diffs.append(str(ROOT / REPO_PLUGINS))
        for name in REPO_FILES:
            mine = ROOT / name
            if not mine.is_file() or not filecmp.cmp(fresh / name, mine, shallow=False):
                diffs.append(str(mine))
    if diffs:
        print("plugins/ is out of date; run python3 scripts/build_clients.py")
        for d in diffs:
            print("  " + str(Path(d).relative_to(ROOT)))
        return 1
    print(f"committed plugins match clients/ (version {version()})")
    return 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return check()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    plugins(OUT)
    shutil.copy2(SRC / "core" / "jezero_client.py", OUT / "jezero_client.py")
    shutil.copy2(PROJECTS, OUT / "jezero_projects.py")
    shutil.copy2(SRC / "README.md", OUT / "README.md")
    marketplaces(OUT, "")
    bundle = DIST / "jezero-client.tar.gz"
    digest = write_bundle(OUT, bundle)

    if (ROOT / REPO_PLUGINS).exists():
        shutil.rmtree(ROOT / REPO_PLUGINS)
    repo_copy(ROOT)
    print(f"built {OUT}, {bundle} and {REPO_PLUGINS}/ (version {version()})")
    print(f"sha256 {digest}  {bundle.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
