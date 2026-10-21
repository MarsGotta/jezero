#!/usr/bin/env python3
"""Assemble the Jezero client bundle from one source of truth.

    clients/core/jezero_client.py   shared core (hooks, upload, setup)
    clients/skill/SKILL.md          instructions for agents, shared by every plugin
    clients/claude-code/            Claude Code plugin manifest and hooks
    clients/codex/                  Codex plugin manifest and hooks
    clients/opencode/jezero.ts      OpenCode plugin

Output:

- dist/client/ (a plugin marketplace for Claude Code and Codex, plus the OpenCode plugin
  and the core) and dist/jezero-client.tar.gz, which `jezero serve` offers at
  /client/jezero-client.tar.gz;
- plugins/ plus .claude-plugin/ and .agents/plugins/ at the repository root, committed,
  so `claude plugin marketplace add OWNER/jezero` and `codex plugin marketplace add
  OWNER/jezero` install straight from the repository.

Run it after changing anything under clients/. `--check` only verifies that the
committed copy matches clients/ (CI runs it).
"""

from __future__ import annotations

import filecmp
import json
import shutil
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


def version() -> str:
    for line in (SRC / "core" / "jezero_client.py").read_text().splitlines():
        if line.startswith("VERSION = "):
            return line.split('"')[1]
    raise SystemExit("VERSION not found in the core")


DESCRIPTION = "Shared read-only memory of past agent work."
REPO_PLUGINS = "plugins"
"""Folder for the committed plugins, at the repository root."""
REPO_FILES = (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")


def plugin(agent: str, target: Path) -> None:
    shutil.copytree(SRC / agent, target)
    (target / "core").mkdir()
    shutil.copy2(SRC / "core" / "jezero_client.py", target / "core" / "jezero_client.py")
    shutil.copy2(PROJECTS, target / "core" / "jezero_projects.py")
    (target / "skills" / "jezero").mkdir(parents=True)
    shutil.copy2(SRC / "skill" / "SKILL.md", target / "skills" / "jezero" / "SKILL.md")
    for manifest in target.glob(".*-plugin/plugin.json"):
        data = json.loads(manifest.read_text())
        data["version"] = version()
        manifest.write_text(json.dumps(data, indent=2) + "\n")


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
    plugin("claude-code", base / "claude-code")
    plugin("codex", base / "codex")
    (base / "opencode").mkdir(parents=True)
    shutil.copy2(SRC / "opencode" / "jezero.ts", base / "opencode" / "jezero.ts")


def repo_copy(root: Path) -> None:
    """The committed copy: plugins/ and the two marketplace files."""
    plugins(root / REPO_PLUGINS)
    marketplaces(root, f"{REPO_PLUGINS}/")


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
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(OUT, arcname="jezero-client")

    if (ROOT / REPO_PLUGINS).exists():
        shutil.rmtree(ROOT / REPO_PLUGINS)
    repo_copy(ROOT)
    print(f"built {OUT}, {bundle} and {REPO_PLUGINS}/ (version {version()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
