# Jezero in Antigravity

Google Antigravity (the app, the IDE and the `agy` CLI) can read Jezero through its MCP
server. One command sets it up for all three, without pasting your key anywhere.

## What you get

- The Jezero tools: `search`, `get_item`, `later`, `timeline` and `list_sources`.
- The `jezero` skill, which tells the agent when to search and how to search well.

What you do not get yet: Antigravity has no Jezero plugin, so there are no hooks. The
agent is not handed recent work when a session starts, and Antigravity sessions are not
sent to Jezero by this setup. The agent searches when the skill or your question leads it
there.

## Before you start

- A Jezero server you can reach from this computer (`https://YOUR-JEZERO`).
- Python 3.9 or newer (`python3`; on Windows, `py`).
- Antigravity installed and opened at least once, so `~/.gemini` exists.

## Install

1. Get the client from your server:

   ```bash
   mkdir -p ~/.jezero-src
   curl -fsSL https://YOUR-JEZERO/client/jezero-client.tar.gz | tar -xz -C ~/.jezero-src
   ```

2. Create this computer's key. Antigravity only reads, so ask for a read-only key:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init \
       --url https://YOUR-JEZERO --device my-laptop --no-upload
   ```

   Send the printed `jezero device approve ... --read-only` line to whoever runs the
   server. It is a fingerprint, not a secret. Skip this step if the computer is already
   connected for another agent.

3. Once approved, check the key and set up Antigravity:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py status
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent antigravity
   ```

   It adds a `jezero` entry to `~/.gemini/config/mcp_config.json`, keeping your other
   servers, and copies the skill to `~/.gemini/config/skills/jezero/`. Both locations are
   read by the app, the IDE and the CLI. Running it again changes nothing.

4. Restart Antigravity.

## Check that it works

- CLI: run `/mcp` (Jezero should be connected) and `/skills` (it should list `jezero`).
- App and IDE: open **Customizations** and look for Jezero under the installed MCP
  servers.

Then ask about something you worked on before, without naming Jezero, for example
"what did we decide about the login flow?". The agent should search Jezero, check for newer
items with `later`, and answer with item ids like `[123]`.

## Good to know

- The key is stored in `mcp_config.json`, which `setup` leaves readable only by you.
  Do not copy that file into a repository.
- Everything that runs Antigravity with the same `~/.gemini` shares this connection,
  including agents you start from scripts or sandboxes. Give those a key of their own
  with only the sources they need (`jezero device approve NAME DIGEST --read-only
  --sources ...`).
- A project can carry its own servers and skills in `<project>/.agents/mcp_config.json`
  and `<project>/.agents/skills/`. Keep keys out of anything you commit.
- Windows: the files live in `%USERPROFILE%\.gemini\config\`.

## Troubleshooting

- `status` fails with HTTP 401: the key is not approved yet, or the server was not
  restarted after approving it.
- `status` works but Antigravity shows Jezero disconnected: make sure Antigravity can
  reach the server too (VPN, private network), then restart it.
- A hand-written entry must use `serverUrl`; Antigravity ignores `url` and `httpUrl`.

## Remove

```bash
agy mcp remove jezero          # or delete the "jezero" entry from mcp_config.json
rm -r ~/.gemini/config/skills/jezero
```
