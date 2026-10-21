#!/usr/bin/env python3
"""Assemble the Jezero client bundle from one source of truth.

    clients/core/jezero_client.py   shared core (hooks, upload, setup)
    clients/skill/SKILL.md          instructions for agents, shared by every plugin
    clients/claude-code/            Claude Code plugin manifest and hooks
    clients/codex/                  Codex plugin manifest and hooks
    clients/opencode/jezero.ts      OpenCode plugin

Output: dist/client/ (a plugin marketplace for Claude Code and Codex, plus the OpenCode
plugin and the core) and dist/jezero-client.tar.gz, which `jezero serve` offers at
/client/jezero-client.tar.gz. Run it after changing anything under clients/.
"""

from __future__ import annotations

import json
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "clients"
DIST = ROOT / "dist"
OUT = DIST / "client"


def version() -> str:
    for line in (SRC / "core" / "jezero_client.py").read_text().splitlines():
        if line.startswith("VERSION = "):
            return line.split('"')[1]
    raise SystemExit("VERSION not found in the core")


def plugin(agent: str) -> None:
    target = OUT / agent
    shutil.copytree(SRC / agent, target)
    (target / "core").mkdir()
    shutil.copy2(SRC / "core" / "jezero_client.py", target / "core" / "jezero_client.py")
    (target / "skills" / "jezero").mkdir(parents=True)
    shutil.copy2(SRC / "skill" / "SKILL.md", target / "skills" / "jezero" / "SKILL.md")
    for manifest in target.glob(".*-plugin/plugin.json"):
        data = json.loads(manifest.read_text())
        data["version"] = version()
        manifest.write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    plugin("claude-code")
    plugin("codex")
    (OUT / "opencode").mkdir()
    shutil.copy2(SRC / "opencode" / "jezero.ts", OUT / "opencode" / "jezero.ts")
    shutil.copy2(SRC / "core" / "jezero_client.py", OUT / "jezero_client.py")
    shutil.copy2(SRC / "README.md", OUT / "README.md")

    description = "Shared read-only memory of past agent work."
    claude_market = OUT / ".claude-plugin" / "marketplace.json"
    claude_market.parent.mkdir()
    claude_market.write_text(
        json.dumps(
            {
                "name": "jezero",
                "owner": {"name": "Jezero"},
                "plugins": [
                    {"name": "jezero", "source": "./claude-code", "description": description}
                ],
            },
            indent=2,
        )
        + "\n"
    )
    codex_market = OUT / ".agents" / "plugins" / "marketplace.json"
    codex_market.parent.mkdir(parents=True)
    codex_market.write_text(
        json.dumps(
            {
                "name": "jezero",
                "interface": {"displayName": "Jezero"},
                "plugins": [
                    {
                        "name": "jezero",
                        "source": {"source": "local", "path": "./codex"},
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
    bundle = DIST / "jezero-client.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(OUT, arcname="jezero-client")
    print(f"built {OUT} and {bundle} (version {version()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
