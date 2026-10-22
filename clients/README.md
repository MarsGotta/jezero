# Jezero client and plugins

The source of the Jezero plugins, which connect a computer's agents to a Jezero server:
they make agents search Jezero by habit and send the computer's sessions to the server.
There is one plugin each for Claude Code, Codex, OpenCode, Hermes Agent and Antigravity,
all thin adapters over one shared core.

To install them, read the guides:

- [Connect an agent](https://github.com/MarsGotta/jezero/blob/main/docs/how-to/connect-agents.md):
  install, check, update and remove the plugin of each agent.
- [Manage computers and keys](https://github.com/MarsGotta/jezero/blob/main/docs/how-to/devices.md):
  give a computer its key, read-only computers, sandboxes, the upload queue.
- [Troubleshooting](https://github.com/MarsGotta/jezero/blob/main/docs/how-to/troubleshooting.md):
  symptoms, causes and fixes.

## What this folder holds

| Path | What it is |
|---|---|
| `core/jezero_client.py` | The client core: creates the device's key, runs the hooks, queues and uploads sessions, registers the MCP server. Python 3.9+, standard library only. |
| `skill/SKILL.md` | The `jezero` skill, shared by every plugin. |
| `claude-code/` | The Claude Code plugin manifest, hooks and MCP bridge configuration (`.mcp.json`). |
| `codex/` | The Codex plugin manifest and hooks. |
| `opencode/` | The OpenCode plugin (`jezero.ts`) and its `package.json`. |
| `hermes/` | The Hermes Agent plugin (`plugin.yaml`, `__init__.py`). |
| `antigravity/` | The Antigravity plugin manifest, hooks and MCP bridge configuration. |

In the client bundle that a server offers at `/client/jezero-client.tar.gz`, this file
sits next to the built result instead: the core (`jezero_client.py` and
`jezero_projects.py`), one folder per plugin, and the marketplace files for Claude Code
and Codex.

## How the plugins are built

`scripts/build_clients.py` assembles every plugin from this folder. It copies the core,
the project rules (`src/jezero/projects.py`, as `jezero_projects.py`) and the skill into
each plugin, and writes the core's `VERSION` into each manifest. The result goes to:

- `plugins/`, `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json` at
  the repository root, committed, so the plugins install straight from GitHub;
- `src/jezero/client_core.py`, the core inside the `jezero` package, for the app;
- `dist/client/` and `dist/jezero-client.tar.gz`, the bundle, not committed.

After changing anything here, rebuild:

```bash
python3 scripts/build_clients.py
```

Never edit `plugins/` by hand. CI runs `python3 scripts/build_clients.py --check` and
fails if the committed copy differs from this folder. Bump `VERSION` in the core for any
change: Claude Code and Codex cache plugins by version.
