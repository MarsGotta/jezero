# Manage computers and keys

Give each computer its own key, decide whether it uploads or only reads, limit what it
can see, and list, rotate or revoke keys. This guide covers both sides: the computer, with
the client core, and the server, with the `jezero device` commands.

## Before you start

- A running server you can reach over HTTPS: see [Run Jezero as a
  service](deploy.md#reach-it-from-other-computers).
- On each computer, the client bundle unpacked in `~/.jezero-src/jezero-client` (see
  [Connect an agent](connect-agents.md#before-you-start)). The commands below call the
  client core there; change the path if yours differs.
- On the server, a shell where `jezero` finds your `jezero.toml` (or pass `-c FILE`).

## How keys work

Each computer makes its own key. The key never leaves the computer except as the
`Authorization: Bearer ***` header of its own requests; the server only keeps its SHA-256
fingerprint.

1. `jezero_client.py init` creates a random key on the computer and prints the
   fingerprint.
2. The admin passes the fingerprint to `jezero device approve`, which adds a
   `[[clients]]` block to `jezero.toml`.
3. The server notices the change within a second and accepts the key: it hashes each key
   it receives and compares the hash with the ones in the config. No restart is needed.

A key is either a *device* (made by `init`, approved with `device approve`) or a plain
client (made by `jezero new-token` for any MCP client, or read from a variable with
`token_env`, see [Connect an agent](connect-agents.md#any-other-mcp-client)). Both are
`[[clients]]` blocks; only a device approved without `--read-only` may upload.

The server re-reads only `[[clients]]` while it runs. `jezero device approve`,
`revoke` and `rotate` change `jezero.toml` in place: they keep every comment and every
other line, check that the result loads, save a copy of the old file as
`jezero.toml.bak-<UTC time>` next to it, and put the old file back if anything fails. A
file that does not load leaves the server with the keys it had, and an error in its log.

## Add a computer

1. On the computer, create its key:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop
   ```

   You should see:

   ```text
   Device key created. The key stays in /home/me/.jezero; the server only keeps its SHA-256 fingerprint.
   Ask the Jezero admin to approve it with:

     jezero device approve laptop SHA256
   ```

   The device name uses lowercase letters, digits, `-` and `_`. A trailing `/mcp` in the
   URL is removed. The URL must be `https://`, or `http://` to this same computer: plain
   HTTP to another computer sends the key in clear, so `init` refuses it unless you add
   `--insecure-http` (for a private network such as a VPN). The client never follows
   redirects either, so use the server's final address. If the server's MCP endpoint is not `/mcp` (`[server].path`), add
   `--mcp-path PATH`. Add `--no-upload` for a [read-only
   computer](#read-only-or-uploading). `jezero connect` does the same from the
   [`jezero` command](app.md), asking for each value; its read-only question defaults to
   yes.

2. Send the printed line to the admin. The fingerprint is not a secret.

3. On the server, approve it:

   ```bash
   jezero device approve laptop SHA256
   ```

   You should see:

   ```text
   approved laptop; the server picks up the new key within a second (backup: /home/me/jezero/jezero.toml.bak-20261022T034500Z)
   ```

   It appends to `jezero.toml` a client block and one disabled connector per agent, each
   reading the device's folder of the inbox, which it creates:

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
   `agent-hermes-laptop` and `agent-antigravity-laptop`. `device approve` refuses a name
   that exists (`error: a client named 'laptop' already exists`).

4. On the computer, check the key:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py status
   ```

   You should see:

   ```text
   key: /home/me/.jezero
   server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"..."}
   queued sessions: 0
   rejected uploads: 0
   ```

5. Once the first sessions have arrived, pilot each connector you need and enable it, as
   for any source:

   ```bash
   jezero run --connector agent-claude-code-laptop --sample 3
   ```

   Then set `enabled = true` in its block. Until a connector is enabled, uploads wait in
   the inbox and no items appear. See [Add and tune sources](sources.md).

Then install the plugins: [Connect an agent](connect-agents.md).

## List computers and keys

On the server:

```bash
jezero device list
```

You should see:

```text
NAME         KIND       UPLOAD  SOURCES                               INBOX
admin        token_env  no      *                                     -
laptop       device     yes     agent-*                               /home/me/jezero/data/inbox/laptop
work-mac     device     no      agent-*                               -
billing-bot  device     yes     agent-codex-*, sandbox-*-billing-bot  /home/me/jezero/data/inbox/billing-bot
laptop connectors: agent-claude-code-laptop (enabled), agent-codex-laptop (disabled), agent-opencode-laptop (disabled), agent-hermes-laptop (disabled), agent-antigravity-laptop (disabled)
billing-bot connectors: sandbox-claude-code-billing-bot (disabled), sandbox-codex-billing-bot (disabled), sandbox-opencode-billing-bot (disabled), sandbox-hermes-billing-bot (disabled), sandbox-antigravity-billing-bot (disabled)
```

- `KIND` is `token_env` for a client whose key comes from an environment variable, and
  `device` for one with a `token_sha256`, which includes keys made with `new-token`.
- `INBOX` is the device's upload folder, for keys that may upload.
- The last lines list the connectors that read each device's inbox folder.

`--json` prints the same as a list of objects, for scripts:

```bash
jezero device list --json
```

You should see, for each client:

```json
{
  "name": "laptop",
  "kind": "device",
  "upload": true,
  "sources": [
    "agent-*"
  ],
  "inbox": "/home/me/jezero/data/inbox/laptop",
  "connectors": [
    {
      "name": "agent-claude-code-laptop",
      "enabled": true
    },
    ...
  ]
}
```

`jezero connector list` shows the connectors themselves, with their last run, last
success, error and items: see [Keep an eye on Jezero](monitor.md#check-the-connectors).

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

- The computer uploads but the server says read-only: every upload gets
  `HTTP 403 this key may not upload`. The client keeps the whole queue, logs it once and
  shows `uploads refused since ...` in `status`, so nothing is lost if the server allows
  uploads later.
- The computer is read-only but the server allows uploads: nothing is sent.

To make an uploading computer read-only:

1. On the computer, add `"upload": false` to `client.json` in its [client
   home](#where-the-key-lives). Sessions already queued stay in `spool/pending/`: delete
   them if they should never be sent.
2. On the server, set `upload = false` in the device's `[[clients]]` block and
   `enabled = false` in its connectors. The server picks the change up within a second.
   Items already stored stay in the library.

To make a read-only computer upload:

1. On the computer, remove `"upload": false` from `client.json`.
2. On the server, remove the old approval and approve again, with the same name and
   fingerprint, without `--read-only`:

   ```bash
   jezero device revoke work-mac
   jezero device approve work-mac SHA256
   ```

   If the device had inbox connectors from an earlier approval, `approve` keeps them
   (still disabled, with their cursors) and says `kept its existing connectors`;
   otherwise it adds them, disabled. Set `enabled = true` on the ones you want again.
   Both commands back the config up first.
3. Send the sessions already on the computer with [`backfill`](#the-upload-queue).

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

To change what a key reads later, edit `sources` in its block: the server applies it
within a second. `jezero device list` shows each key's sources, and `jezero stats` the
source names in the library. See [`[[clients]]`](../reference/configuration.md#clients)
and [Each client sees only its
sources](../explanation/security.md#each-client-sees-only-its-sources).

## Where the key lives

The client home is the folder with the key and everything the client keeps:

| File | What it holds |
|---|---|
| `token` | The key, readable only by you. Never print or copy it. |
| `client.json` | The server URL, the device name, `mcp_path` when the server's MCP path is not `/mcp`, `"upload": false` on a read-only computer, and optional [project rules](#projects). |
| `spool/` | The upload queue. |
| `state.json` | What was uploaded or rejected already, and which sessions got their first-prompt context. |
| `client.log` | Errors from hooks and uploads. |
| `ca.pem` | System certificates exported on macOS, when needed (see [Troubleshooting](troubleshooting.md#tls-or-certificate-errors)). |
| `core/` | A copy of the client core, written by `setup`: the MCP bridge and the OpenCode plugin run it. |

The client looks for its home in this order and uses the first that applies:

1. `JEZERO_CLIENT_HOME`, when set.
2. The agent's own folder, when it holds a key and the command runs for that agent (the
   plugins' hooks and bridge, and `status`, `flush`, `setup` and `backfill` with
   `--agent`): `~/.gemini/config/jezero` for Antigravity, `jezero` inside
   `CLAUDE_CONFIG_DIR` for Claude Code, `jezero` inside `CODEX_HOME` for Codex.
3. `jezero` inside `CLAUDE_CONFIG_DIR`, then inside `CODEX_HOME`, when that variable is
   set and the folder holds a key.
4. `~/.jezero`.

`status` prints the home it used on its first line (`key: /home/me/.jezero`). The OpenCode
plugin looks in `JEZERO_CLIENT_HOME`, then in `jezero` inside OpenCode's config folder
(`~/.config/opencode/jezero`, or under `XDG_CONFIG_HOME`), then in `~/.jezero`.

## Agents in a sandbox

Agents run by an orchestrator often get their own config folder and cannot read your
home. Give each one its own key, inside its config folder, so it needs nothing else:

1. Create the key there. `init` writes to that folder only when `JEZERO_CLIENT_HOME`
   names it; afterwards the client finds the key on its own:

   ```bash
   JEZERO_CLIENT_HOME=/home/me/agents/billing/codex/jezero python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device billing-bot
   ```

   | Agent | Folder for its key |
   |---|---|
   | Claude Code | `$CLAUDE_CONFIG_DIR/jezero` |
   | Codex | `$CODEX_HOME/jezero` |
   | OpenCode | `$XDG_CONFIG_HOME/opencode/jezero` |
   | Antigravity | `~/.gemini/config/jezero` |
   | Any agent | any folder, with `JEZERO_CLIENT_HOME` set in the agent's environment |

2. Approve it with only the sources the agent needs, and a prefix if its work should stay
   apart (see [Limit what a key can read](#limit-what-a-key-can-read)).

3. Register the MCP server with the agent's own environment, so `setup` writes into the
   agent's configuration and uses its key:

   ```bash
   CODEX_HOME=/home/me/agents/billing/codex python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent codex
   ```

   You should see:

   ```text
   codex: MCP server jezero added to config.toml (stdio bridge; key in /home/me/agents/billing/codex/jezero)
   ```

   The block it writes names the key's folder, since Codex passes few variables to MCP
   servers:

   ```toml
   [mcp_servers.jezero]
   command = "python3"
   args = ["/home/me/agents/billing/codex/jezero/core/jezero_client.py", "mcp", "--agent", "codex"]
   env = { JEZERO_CLIENT_HOME = "/home/me/agents/billing/codex/jezero" }
   ```

4. Check it with the same environment:

   ```bash
   CODEX_HOME=/home/me/agents/billing/codex python3 ~/.jezero-src/jezero-client/jezero_client.py status --agent codex
   ```

   The first line, `key: /home/me/agents/billing/codex/jezero`, confirms which key it
   used.

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

You should see:

```text
{"sent": 1, "unchanged": 0, "gone": 0, "failed": 0, "rejected": 0}
```

`gone` counts sessions whose file was deleted before it was sent. When `failed` is not 0,
read `client.log` in the client home. Both `status` and `flush` take `--agent NAME` to
use the key and queue of that agent (see [Where the key lives](#where-the-key-lives)).

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
the plugin). `--days` defaults to 90. Each agent's sessions go to the queue of the key
that agent uses.

### When the server says no

What the client does depends on the answer:

| Answer | Meaning | What the client does |
|---|---|---|
| 401 `unknown token` | The key is not approved yet, was revoked, or was replaced by a rotation. | Stops, keeps the whole queue, logs it once. `status`: `uploads refused since ...`. |
| 403 `this key may not upload` | The key may only read. | The same. |
| 400, 413, 415 | The server will never accept this file as it is: a path it does not take, a file over [`inbox.max_mb`](../reference/configuration.md#inbox) (64 MB by default), an encoding it does not read. | Records the file as rejected and moves on to the next one. It is sent again only if the file changes. `status`: `rejected uploads: N`, with the newest five and their reason. |
| Network error, 421, 5xx | The server cannot be reached or is failing. | Stops and keeps the queue for next time. |

So a computer that uploads before its key is approved, or while it is read-only on the
server, loses nothing: once the admin approves the key for uploads, the next flush sends
everything. See [Troubleshooting](troubleshooting.md#uploads-refused).

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

Give a computer a new key, keeping its name, so its inbox folder, connectors and cursors
stay the same:

1. On the computer, make a new key. It replaces the old one in the client home at once;
   the queue is kept:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop --force
   ```

   Since the device name is the same, it prints the line for the admin:

   ```text
   The server already knows this device: ask the Jezero admin to switch keys with:

     jezero device rotate laptop SHA256

   The old key stops working as soon as that runs.
   ```

   > [!NOTE]
   > `init --force` keeps the computer's other settings, such as `projects`. Pass
   > `--no-upload`, `--mcp-path` and `--insecure-http` again if the computer used them.

2. On the server, replace the fingerprint:

   ```bash
   jezero device rotate laptop SHA256
   ```

   You should see:

   ```text
   rotated laptop: the old key stops working within a second (backup: /home/me/jezero/jezero.toml.bak-20261022T034510Z)
   ```

   It changes only the `token_sha256` line of the device's block. A client whose key
   comes from `token_env` cannot be rotated this way: `error: admin reads its key from
   JEZERO_ADMIN_TOKEN; change that variable instead`.

3. On the computer, check with `jezero_client.py status`. The old key now gets
   `HTTP 401 unknown token`.

The MCP bridge (Claude Code, Codex, Antigravity), the Hermes plugin and the hooks read
the key file for each request, so they use the new key at once. OpenCode reads it when it
starts: restart it. Update any MCP client you configured by hand with the key. If an
older client wrote the key into Claude Code's or Codex's configuration, run
`jezero_client.py setup --agent claude-code` (or `codex`) once: it replaces that entry
with the bridge.

Between steps 1 and 2 the computer cannot reach Jezero, and its uploads wait in the queue
(the server answers 401). They go out at the next flush after the rotation.

## Revoke a computer

On the server:

```bash
jezero device revoke laptop
```

You should see:

```text
revoked laptop: its key stops working within a second (backup: /home/me/jezero/jezero.toml.bak-20261022T034511Z)
disabled its connectors (kept, with their cursors): agent-claude-code-laptop, agent-codex-laptop, agent-opencode-laptop, agent-hermes-laptop, agent-antigravity-laptop
items already stored are kept: 1 from its connectors
```

It removes the device's `[[clients]]` block, with its comment, and sets
`enabled = false` in the connectors that read its inbox folder, which keep their cursors.
To delete those connector blocks too, add `--remove-connectors`:

```bash
jezero device revoke billing-bot --remove-connectors
```

You should see:

```text
revoked billing-bot: its key stops working within a second (backup: /home/me/jezero/jezero.toml.bak-20261022T034511Z-1)
removed its connectors: sandbox-claude-code-billing-bot, sandbox-codex-billing-bot, sandbox-opencode-billing-bot, sandbox-hermes-billing-bot, sandbox-antigravity-billing-bot
items already stored are kept: 0 from its connectors
```

Either way, the items already in the library stay, and so do the uploaded files in its
inbox folder (`data/inbox/NAME` by default) until you delete them. `revoke` works for any
client, `token_env` ones included.

On the computer, requests now get `HTTP 401 unknown token`, and its queue stays where it
is. Remove the plugins (see [Connect an agent](connect-agents.md)), the MCP registrations
and the client home.

If a key may have leaked, revoke it first, then give the computer a new key with [Add a
computer](#add-a-computer), or [rotate](#rotate-a-key) instead of revoking when the
computer itself is safe.

## What the server stores

- For each key: its name, the SHA-256 of the key, the sources it may read and whether it
  may upload, in `jezero.toml`. Never the key itself.
- For each upload: the transcript as the computer sent it, in
  `<inbox>/<device>/<agent>/...`, and next to it a `.project` file with the project the
  computer found. Secrets are redacted when a connector reads the file, before anything
  is summarised or stored; the raw file is kept as sent.

On the computer, the key sits in `token` only. The bridge that `setup` and the Claude Code
and Antigravity plugins register reads it from there, so no agent's configuration holds
it; OpenCode's plugin passes it to OpenCode in memory. See
[Keys](../explanation/security.md#keys) and [Uploading is separate from
reading](../explanation/security.md#uploading-is-separate-from-reading).

## Next steps

- [Connect an agent](connect-agents.md): install the plugins once the computer has a key.
- [Troubleshooting](troubleshooting.md): 401, 403, refused and rejected uploads, items
  without a project.
- [Command reference](../reference/cli.md#jezero-device): every option of the `jezero
  device` commands and of the client core.
