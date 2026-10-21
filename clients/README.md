# Jezero client and plugins

Connect a computer to a Jezero server. The plugins make agents use Jezero by habit and
send the computer's sessions to the server, so past work from every machine ends up in
the same library.

| Agent | What the plugin adds |
|---|---|
| Claude Code | hooks (session start, first prompt, stop, session end) and the `jezero` skill |
| Codex | the same hooks and skill (Codex asks you to trust the hooks once, in `/hooks`) |
| OpenCode | the protocol in the system prompt and upload when a session goes idle |

All three share one core, `jezero_client.py` (Python 3.9+, standard library only).

## What the hooks do

- **Session start**: adds a short protocol (search Jezero before non-trivial work, check
  `later` before relying on a decision, cite ids, never guess) and the five most recent
  items of the current project (see [Projects](#projects)).
- **First prompt of a session**: searches Jezero with the prompt and adds up to three
  related items, if any match well.
- **Stop / session end**: queues the transcript and uploads it in a detached process.
  Offline? It stays queued and goes out next time.

A hook never blocks or breaks the agent: network calls time out after a few seconds and
errors only go to `~/.jezero/client.log`.

## Install on a computer

1. Get the bundle from your server and unpack it:

   ```bash
   mkdir -p ~/.jezero-src
   curl -fsSL https://YOUR-JEZERO/client/jezero-client.tar.gz | tar -xz -C ~/.jezero-src
   ```

2. Create this computer's key. The key stays on the computer; only its fingerprint is
   shown:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device my-laptop
   ```

   Send the printed `jezero device approve ...` line to whoever runs the server. It is a
   SHA-256 fingerprint, not a secret.

3. Once approved, check it and register the MCP server in your agents:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py status
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup \
       --opencode-plugin ~/.jezero-src/jezero-client/opencode/jezero.ts
   ```

4. Install the plugins, from GitHub (updates arrive with `marketplace update`) or from
   the bundle you unpacked in step 1 (use `~/.jezero-src/jezero-client` instead of
   `MarsGotta/jezero`; handy for a server that runs its own changes):

   - Claude Code: `claude plugin marketplace add MarsGotta/jezero` and then
     `claude plugin install jezero@jezero` (or `/plugin` inside Claude Code).
   - Codex: `codex plugin marketplace add MarsGotta/jezero`, install Jezero from
     `/plugins`, then trust its hooks in `/hooks`.
   - OpenCode: done by `setup` in step 3 (`~/.config/opencode/plugins/jezero.ts`).

5. Optional: send the sessions already on this computer.

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py backfill --days 90
   ```

## Projects

Each session is tagged with a project, found on this computer: the git repository it ran
in (for a worktree, the main repository), or the folder below `projects`, `code`, `dev`,
`src`, `repos`... To set your own, add to `~/.jezero/client.json`:

```json
"projects": {"paths": {"~/clients/acme": "acme"}, "roots": ["work"], "aliases": {"acme-web": "acme"}}
```

## Server side

`jezero device approve NAME SHA256` adds the device as a client with `upload = true`
and three disabled connectors that read its inbox (`agent-claude-code-NAME`,
`agent-codex-NAME`, `agent-opencode-NAME`). Run a pilot (`jezero run --connector ...
--sample 3`) before enabling them. Restart `jezero serve` so it accepts the new key.

Uploads land in `<inbox>/<device>/<agent>/...`, mirroring each agent's own store, and
the usual connectors summarise them. The MCP server stays read-only.

## Development

Edit `clients/`, then run `python3 scripts/build_clients.py`; it copies the core and the
skill into every plugin, so there is a single source for both. It writes the server
bundle (`dist/`, not committed) and the installable copy at the repository root
(`plugins/`, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`),
which is committed so the plugins install from GitHub. Never edit `plugins/` by hand:
CI runs `python3 scripts/build_clients.py --check` and fails if it differs from
`clients/`. Bump `VERSION` in the core for any change, because Claude Code and Codex
cache plugins by version.
