# Manage computers and keys

Give each computer its own key, decide whether it uploads or only reads, limit what it
can see, and rotate or remove keys. This guide covers both sides: the computer, with the
client core, and the server, with `jezero device approve`.

## Before you start

- A running server you can reach over HTTPS: see [Run Jezero as a
  service](deploy.md#reach-it-from-other-computers).
- On each computer, the client bundle unpacked in `~/.jezero-src/jezero-client` (see
  [Connect an agent](connect-agents.md#before-you-start)). The commands below call the client core
  there; change the path if yours differs.
- On the server, a shell where `jezero` finds your `jezero.toml` (or pass `-c FILE`).

## How keys work

Each computer makes its own key. The key never leaves the computer except as the
`Authorization: Bearer ***` header of its own requests; the server only keeps its SHA-256
fingerprint.

1. `jezero_client.py init` creates a random key on the computer and prints the
   fingerprint.
2. The admin passes the fingerprint to `jezero device approve`, which adds a
   `[[clients]]` block to `jezero.toml`.
3. After a restart, the server accepts the key: it hashes each key it receives and
   compares the hash with the ones in the config.

A key is either a *device* (made by `init`, approved with `device approve`) or a plain
client (made by `jezero new-token` for any MCP client, see [Connect an
agent](connect-agents.md#any-other-mcp-client)). Both are `[[clients]]` blocks; only a device may
upload.

## Add a computer

1. On the computer, create its key:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop
   ```

   You should see:

   ```text
   Device key created in /home/me/.jezero (the key itself never leaves this computer).
   Ask the Jezero admin to approve it with:

     jezero device approve laptop SHA256
   ```

   The device name uses lowercase letters, digits, `-` and `_`. A trailing `/mcp` in the
   URL is removed. Add `--no-upload` for a [read-only computer](#read-only-or-uploading).
   `jezero connect` does the same from the [`jezero` command](app.md), asking for each
   value; its read-only question defaults to yes.

2. Send the printed line to the admin. The fingerprint is not a secret.

3. On the server, approve it:

   ```bash
   jezero device approve laptop SHA256
   ```

   You should see:

   ```text
   approved laptop; restart the MCP server so it accepts the new key
   ```

   It appends to `jezero.toml` a client block and one disabled connector per agent, each
   reading the device's folder of the inbox:

   ```toml
   [[clients]]
   # Device laptop: reads Jezero and uploads its sessions to the inbox.
   name = "laptop"
   token_sha256 = "SHA256"
   sources = ["agent-*"]
   upload = true

   [[connectors]]
   # Sessions uploaded by laptop. Disabled until the pilot passes.
   name = "agent-claude-code-laptop"
   type = "claude_code"
   enabled = false
   interval_minutes = 15
   [connectors.options]
   projects_dir = "/home/me/jezero/data/inbox/laptop/claude-code/projects"
   ...
   ```

   The other connectors are `agent-codex-laptop`, `agent-opencode-laptop`,
   `agent-hermes-laptop` and `agent-antigravity-laptop`.

4. Restart the server, so it reads the new block:

   ```bash
   systemctl --user restart jezero-serve.service
   ```

5. On the computer, check the key:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py status
   ```

   You should see:

   ```text
   server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"..."}
   queued sessions: 0
   ```

6. Once the first sessions have arrived, pilot each connector you need and enable it, as
   for any source:

   ```bash
   jezero run --connector agent-claude-code-laptop --sample 3
   ```

   Then set `enabled = true` in its block. Until a connector is enabled, uploads wait in
   the inbox and no items appear. See [Add and tune sources](sources.md).

Then install the plugins: [Connect an agent](connect-agents.md).

## Read-only or uploading

A computer either uploads its sessions or only reads Jezero. Two switches decide it, and
they should agree:

| Side | Uploading | Read-only |
|---|---|---|
| Computer | `init` | `init --no-upload`: writes `"upload": false` in `client.json`; hooks give context but queue nothing |
| Server | `device approve NAME SHA256` | `device approve NAME SHA256 --read-only`: `upload = false`, no connectors |

Make a computer read-only when its sessions already reach Jezero another way, such as the
server's own computer, whose connectors read the agents' folders directly, or when its
sessions should not be stored at all.

If the two sides disagree:

- The computer uploads but the server says read-only: every upload is refused with
  `HTTP 403 this key may not upload`. The client drops the refused files and marks them
  as done, so they are not sent again later.
- The computer is read-only but the server allows uploads: nothing is sent.

To make an uploading computer read-only:

1. On the computer, add `"upload": false` to `client.json` in its [client
   home](#where-the-key-lives).
2. On the server, set `upload = false` in its `[[clients]]` block, set `enabled = false`
   in its connectors, and restart. Items already stored stay in the library.

To make a read-only computer upload:

1. On the computer, remove `"upload": false` from `client.json`.
2. On the server, delete the device's `[[clients]]` block and approve it again, with the
   same name and fingerprint, without `--read-only`. A read-only approval wrote no
   connectors, so this adds them without duplicates. Restart the server.
3. Send the sessions already on the computer with [`backfill`](#the-upload-queue).
   Sessions refused while the key was read-only count as sent: delete `state.json` in the
   client home first to send them anyway.

## Limit what a key can read

Each `[[clients]]` block lists the sources its key may read, as glob patterns on source
names. Every tool applies the list: a key never sees an item from another source.

- `device approve` gives `sources = ["agent-*"]` by default: the sessions of every agent
  on every computer. A device always sees its own sources, which are added when the list
  does not cover them.
- `--sources` sets the list:

  ```bash
  jezero device approve work-mac SHA256 --sources "agent-*-work-mac" "agent-codex-*"
  ```

- `--source-prefix` names the device's own sources `PREFIX-AGENT-DEVICE` instead of
  `agent-AGENT-DEVICE`, so keys that read `agent-*` do not see them. Use it for agents
  whose work should stay apart, such as agents in a sandbox:

  ```bash
  jezero device approve billing-bot SHA256 --source-prefix sandbox --sources "agent-codex-*"
  ```

  The block then reads `sources = ["agent-codex-*", "sandbox-*-billing-bot"]`, and the
  connectors are named `sandbox-claude-code-billing-bot` and so on. The prefix is one
  short lowercase word.

To change what a key reads later, edit `sources` in its block and restart the server. To
see the source names, run `jezero stats` on the server. See
[`[[clients]]`](../reference/configuration.md#clients) and [Each client sees only its
sources](../explanation/security.md#each-client-sees-only-its-sources).

## Where the key lives

The client home is the folder with the key and everything the client keeps:

| File | What it holds |
|---|---|
| `token` | The key, readable only by you. Never print or copy it. |
| `client.json` | The server URL, the device name, `"upload": false` on a read-only computer, and optional [project rules](#projects). |
| `spool/` | The upload queue. |
| `state.json` | What was uploaded already, and which sessions got their first-prompt context. |
| `client.log` | Errors from hooks and uploads. |
| `ca.pem` | System certificates exported on macOS, when needed (see [Troubleshooting](troubleshooting.md#tls-or-certificate-errors)). |
| `core/` | A copy of the client core, written by `setup --agent opencode`. |

The client looks for its home in this order and uses the first that applies:

1. `JEZERO_CLIENT_HOME`, when set.
2. Antigravity only: `~/.gemini/config/jezero`, when it holds a key.
3. `jezero` inside `CLAUDE_CONFIG_DIR`, then inside `CODEX_HOME`, when that variable is
   set and the folder holds a key.
4. `~/.jezero`.

The OpenCode plugin looks in `JEZERO_CLIENT_HOME`, then in `jezero` inside OpenCode's
config folder (`~/.config/opencode/jezero`, or under `XDG_CONFIG_HOME`), then in
`~/.jezero`.

## Agents in a sandbox

Agents run by an orchestrator often get their own config folder and cannot read your
home. Give each one its own key, inside its config folder, so it needs nothing else:

1. Create the key there. `init` writes to that folder only when `JEZERO_CLIENT_HOME`
   names it; afterwards the client finds the key on its own:

   ```bash
   JEZERO_CLIENT_HOME=/home/me/agents/billing/claude/jezero python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device billing-bot
   ```

   | Agent | Folder for its key |
   |---|---|
   | Claude Code | `$CLAUDE_CONFIG_DIR/jezero` |
   | Codex | `$CODEX_HOME/jezero` |
   | OpenCode | `$XDG_CONFIG_HOME/opencode/jezero` |
   | Antigravity | `~/.gemini/config/jezero` |
   | Any agent | any folder, with `JEZERO_CLIENT_HOME` set in the agent's environment |

2. Approve it with only the sources the agent needs, and a prefix if its work should stay
   apart (see [Limit what a key can read](#limit-what-a-key-can-read)). Restart the
   server.

3. Register the MCP server with the agent's own environment, so `setup` writes into the
   agent's configuration and uses its key:

   ```bash
   CLAUDE_CONFIG_DIR=/home/me/agents/billing/claude python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent claude-code
   ```

Each key has its own queue in its own folder, so the sandbox uploads its sessions under
its own device name.

## The upload queue

The stop and session-end hooks do not upload by themselves. They write a small pointer
to `spool/pending/` and start a detached `flush`, which sends each queued session that
changed since its last upload. A flush stops at the first network error and keeps the
rest for next time; every session start tries again. `state.json` remembers the size and
time of each file sent, so an unchanged session is never sent twice.

See how many sessions wait:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py status
```

Send them now:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py flush
```

You should see, in a terminal:

```text
{"sent": 1, "unchanged": 0, "gone": 0, "failed": 0}
```

`gone` counts sessions whose file was deleted before it was sent. When `failed` is not 0,
read `client.log` in the client home.

Send sessions that were on the computer before the plugins:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py backfill --days 90
```

You should see:

```text
queued 12 sessions; uploading in the background
```

Without `--agent` it queues Claude Code and Codex sessions. Repeat `--agent` for others:
`hermes` and `antigravity` work too, `opencode` does not (its sessions only arrive through
the plugin). `--days` defaults to 90.

The server refuses some files for good: a path it does not accept, a key that may not
upload, or a file over [`inbox.max_mb`](../reference/configuration.md#inbox) (64 MB by
default). The client logs the refusal and drops the file.

## Projects

Each upload carries the project the computer found for the session, from the folder it
ran in:

1. Folders you map in `client.json` (`paths`).
2. The git repository the folder is in (for a worktree, the main repository).
3. The folder below a common root (`projects`, `code`, `dev`, `src`, `repos`...).

To set your own rules, add a `projects` object to `client.json`:

```json
{
  "url": "https://YOUR-JEZERO",
  "device": "laptop",
  "projects": {
    "paths": {"~/clients/shop-site": "shop"},
    "aliases": {"atlas-wt": "atlas"}
  }
}
```

`roots` replaces the list of common root folders; leave it out to keep the defaults. The
rules apply to sessions uploaded from then on. The server has the same rules for its own
sources: see [`[projects]`](../reference/configuration.md#projects).

## Rotate a key

There is no rotate or revoke command: `jezero device approve` only adds keys. Rotate by
hand, keeping the device name, so the inbox folder and connectors stay the same:

1. On the computer, make a new key. It replaces the old one in the client home at once;
   the queue is kept:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop --force
   ```

2. On the server, open `jezero.toml`, find the device's `[[clients]]` block and replace
   the value of `token_sha256` with the new fingerprint. Do not run `device approve`
   again: it refuses a name that exists (`error: a client named 'laptop' already
   exists`).

3. Restart the server. The old key now gets `HTTP 401 unknown token`.

4. Replace the copies of the key that `setup` wrote into agent configurations:

   ```bash
   claude mcp remove jezero --scope user
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent claude-code
   codex mcp remove jezero
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent codex
   ```

   OpenCode, Hermes Agent and Antigravity read the key file when they start: restart
   them. Update any MCP client you configured by hand.

5. Check with `jezero_client.py status`.

Between steps 1 and 3 the computer's agents cannot reach Jezero. Agree on a moment with
the admin.

## Remove a computer

1. On the server, delete the device's `[[clients]]` block and restart the server. The key
   stops working at once (`HTTP 401 unknown token`).
2. Set `enabled = false` in its connectors, or delete them. Items already in the library
   stay, and so do the uploaded files in its inbox folder (`data/inbox/NAME` by default)
   until you delete them.
3. On the computer, remove the plugins (see [Connect an agent](connect-agents.md)), the MCP
   registrations and the client home.

If a key may have leaked, do step 1 first, then give the computer a new key with
[Add a computer](#add-a-computer) under a new name, or with [Rotate a
key](#rotate-a-key).

## What the server stores

- For each key: its name, the SHA-256 of the key, the sources it may read and whether it
  may upload, in `jezero.toml`. Never the key itself.
- For each upload: the transcript as the computer sent it, in
  `<inbox>/<device>/<agent>/...`, and next to it a `.project` file with the project the
  computer found. Secrets are redacted when a connector reads the file, before anything
  is summarised or stored; the raw file is kept as sent.

On the computer, the key sits in `token` and, after `setup`, in the MCP configuration of
Claude Code and Codex. See [Keys](../explanation/security.md#keys) and [Uploading is
separate from reading](../explanation/security.md#uploading-is-separate-from-reading).

## Next steps

- [Connect an agent](connect-agents.md): install the plugins once the computer has a key.
- [Troubleshooting](troubleshooting.md): 401, 403, uploads that wait, items without a
  project.
- [Command reference](../reference/cli.md#jezero-device-approve): every option of
  `device approve` and of the client core.
