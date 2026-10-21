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
  items of the current project (taken from the working directory, `.../projects/<name>`).
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

4. Install the plugins:

   - Claude Code: `claude plugin marketplace add ~/.jezero-src/jezero-client` and then
     `claude plugin install jezero@jezero` (or `/plugin` inside Claude Code).
   - Codex: `codex plugin marketplace add ~/.jezero-src/jezero-client`, install Jezero
     from `/plugins`, then trust its hooks in `/hooks`.
   - OpenCode: done by `setup` in step 3 (`~/.config/opencode/plugins/jezero.ts`).

5. Optional: send the sessions already on this computer.

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py backfill --days 90
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
skill into every plugin, so there is a single source for both.
