# Connect an agent

Install the Jezero plugin in each agent you use, so the agent searches Jezero by habit and
sends its sessions to your server. This guide covers Claude Code, Codex, OpenCode, Hermes
Agent, Antigravity, any other MCP client, and the app's Agents view.

## Before you start

- This computer is connected: it has a key and the admin has approved it. See [Connect a
  second computer](../getting-started/connect-a-computer.md), or [Manage computers and
  keys](devices.md#add-a-computer) for the details.
- `python3` (3.9 or newer) is on the agent's `PATH`: every plugin runs the
  [client core](../reference/client.md) with it.
- The client bundle is unpacked in `~/.jezero-src/jezero-client`. The commands below use
  that path; change it if you unpacked the bundle elsewhere. To get it:

  ```bash
  mkdir -p ~/.jezero-src
  curl -fsSL https://YOUR-JEZERO/client/jezero-client.tar.gz | tar -xz -C ~/.jezero-src
  ```

Check the connection before installing anything:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py status
```

You should see:

```text
server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"..."}
queued sessions: 0
```

A [read-only computer](devices.md#read-only-or-uploading) shows
`server https://YOUR-JEZERO: read-only, N readable sources` instead.

## What each agent gets

Every plugin is a thin adapter over the same client core, so they behave alike. They
differ in what their agent lets a plugin do.

| Agent | Tools | At session start | On the first prompt | Upload | Skill | Older sessions |
|---|---|---|---|---|---|---|
| Claude Code | MCP server, registered by `setup` | protocol and recent work | related past work | at each stop and at session end | `jezero` | `backfill` |
| Codex | MCP server, registered by `setup` | protocol and recent work | related past work | at each stop and at session end | `jezero` | `backfill` |
| OpenCode | MCP server, registered by the plugin | protocol and recent work, in the system prompt | related past work, in the system prompt | when the session goes idle | `jezero` | none |
| Hermes Agent | `jezero_*` tools | nothing | recent work and related past work, added to the first message | at the end of a turn and when the session closes | `jezero:jezero` | `backfill --agent hermes` |
| Antigravity | MCP server through a local bridge | protocol and recent work | related past work, before the first model call | when the conversation stops | `jezero` | `backfill --agent antigravity` |

- **Tools**: `search`, `get_item`, `later`, `timeline` and `list_sources`. Hermes Agent
  gets the same tools under the names `jezero_search`, `jezero_get_item` and so on. See
  [MCP tools](../reference/mcp-tools.md).
- **Protocol**: a short text that tells the agent to search Jezero before non-trivial
  work, to check `later` before relying on a decision, and to cite item ids. Print it with
  `python3 ~/.jezero-src/jezero-client/jezero_client.py protocol`.
- **Recent work**: the five newest items of the [project](devices.md#projects) the
  session runs in. Nothing is added when the folder belongs to no project.
- **Related past work**: up to three items whose title or summary contains at least two
  distinctive words of the first prompt. See [Context on the first
  prompt](../explanation/search.md#context-on-the-first-prompt).
- **Upload**: the session is queued and sent by a background process, so the agent never
  waits. A stop sends at most once every 15 minutes per session (Hermes Agent: once every
  10 minutes); the end of a session always sends. What is still queued goes out at the
  next session start. See [The upload queue](devices.md#the-upload-queue).
- **Skill**: when to search and how to search well, for agents that load skills.

Context hooks wait at most 2.5 seconds for the server (`JEZERO_HOOK_TIMEOUT`). A hook never
blocks or breaks the agent: errors go to `client.log` in the [client
home](devices.md#where-the-key-lives).

A plugin installed on a computer without a key stays quiet: the tools are missing or
answer that Jezero is not connected, and nothing is uploaded.

## Claude Code

The plugin brings the hooks and the skill. The MCP server is registered separately, by
the client core.

1. Install the plugin:

   ```bash
   claude plugin marketplace add MarsGotta/jezero
   claude plugin install jezero@jezero
   ```

   You can also run `/plugin` inside Claude Code. To install from your server's bundle
   instead of GitHub, use `claude plugin marketplace add ~/.jezero-src/jezero-client`.

2. Register the MCP server:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent claude-code
   ```

   You should see:

   ```text
   claude: MCP registered
   ```

   It runs `claude mcp add --scope user --transport http jezero https://YOUR-JEZERO/mcp`
   with the header `Authorization: Bearer ***`, so Claude Code's user configuration holds a
   copy of the key. Run it with the same `CLAUDE_CONFIG_DIR` as the agent, if you set one.

3. Restart Claude Code.

For `claude -p` and other runs where nobody can answer a permission prompt, allow the
Jezero tools in `~/.claude/settings.json` (or in `settings.json` inside
`CLAUDE_CONFIG_DIR`):

```json
{
  "permissions": {
    "allow": ["mcp__jezero"]
  }
}
```

For a single run, `claude -p --allowedTools mcp__jezero "..."` does the same.

**Check it works.** `claude plugin list` shows `jezero@jezero` as enabled, and
`claude mcp list` shows `jezero` as connected. Start Claude Code in a project folder and
ask about something you worked on before, without naming Jezero. The agent should call
`search`, then `later`, and cite item ids like `[123]`.

**Update.** Claude Code caches plugins by version:

```bash
claude plugin marketplace update jezero
claude plugin update jezero@jezero
```

Restart Claude Code to load the new version.

**Remove.**

```bash
claude plugin uninstall jezero@jezero
claude mcp remove jezero --scope user
```

## Codex

Like Claude Code: the plugin brings the hooks and the skill, and `setup` registers the
MCP server.

1. Install the plugin:

   ```bash
   codex plugin marketplace add MarsGotta/jezero
   codex plugin add jezero@jezero
   ```

   You can also install Jezero from `/plugins` inside Codex. To install from your
   server's bundle, use `codex plugin marketplace add ~/.jezero-src/jezero-client`.

2. Register the MCP server:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent codex
   ```

   You should see:

   ```text
   codex: MCP added to config.toml
   ```

   It appends this block to `config.toml` in `CODEX_HOME` (default `~/.codex`), with the
   key in the header:

   ```toml
   [mcp_servers.jezero]
   url = "https://YOUR-JEZERO/mcp"
   http_headers = { Authorization = "Bearer ***" }
   ```

3. Trust the hooks: open Codex, run `/hooks` and trust the Jezero hooks. Codex does not
   run a plugin's hooks until you do, so until then there is no context and no upload.

**Check it works.** `codex plugin list` shows `jezero@jezero  installed, enabled` with
its version, and `codex mcp list` lists `jezero`. Then ask about past work in a project
folder, as for Claude Code.

**Update.**

```bash
codex plugin marketplace upgrade jezero
codex plugin add jezero@jezero
```

If Codex lists the hooks as untrusted again in `/hooks`, trust them again.

**Remove.**

```bash
codex plugin remove jezero@jezero
codex mcp remove jezero
```

## OpenCode

The OpenCode plugin is one folder with its own copy of the core and the skill. It
registers the MCP server itself, when OpenCode loads it, so your `opencode.json` is never
edited.

1. Install it with `setup`:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent opencode --opencode-plugin ~/.jezero-src/jezero-client/opencode
   ```

   It writes `~/.config/opencode/plugins/jezero.ts`, pointing at a copy of the core in
   `~/.jezero/core/`, and the skill in `~/.config/opencode/skills/jezero/`. It follows
   `XDG_CONFIG_HOME`, and skips OpenCode when neither `~/.config/opencode` nor the
   `opencode` command exists.

   Or, from a clone of the repository, load the plugin folder in `opencode.json`:

   ```json
   {
     "plugin": ["file:///home/me/jezero/plugins/opencode/jezero.ts"]
   }
   ```

2. Restart OpenCode.

The plugin adds a remote MCP server named `jezero` with the key in its header, read from
the key file each time OpenCode starts. If your configuration already has an `mcp.jezero`
entry, the plugin leaves it alone.

**Check it works.** Start OpenCode in a project folder and ask which Jezero sources it
can read: it should call `list_sources` on the `jezero` server. If the key was missing
when OpenCode started, the server is not registered: restart OpenCode after `init`.

**Update.** Download the bundle again and repeat step 1. With a clone, pull it.

**Remove.** Delete `~/.config/opencode/plugins/jezero.ts`,
`~/.config/opencode/skills/jezero/` and `~/.jezero/core/`, or remove the `plugin` line.

OpenCode sessions reach Jezero only through the plugin, when a session goes idle:
`backfill` cannot send sessions from before it was installed.

## Hermes Agent

The Hermes plugin brings its own tools and calls the server with this computer's key.

1. Install and enable it:

   ```bash
   hermes plugins install MarsGotta/jezero/plugins/hermes --enable
   ```

   Hermes installs plugins per profile: run it in each profile that should use Jezero.

2. Restart Hermes, including the gateway if you run one.

The `jezero_*` tools appear only once Hermes finds a key: in `~/.jezero`, or in the
folder named by `JEZERO_CLIENT_HOME` in the environment Hermes runs in. On the first turn
of a session the plugin adds recent work and related past work to your message (Hermes
keeps the system prompt unchanged). Scheduled jobs and subagent sessions are never
uploaded.

**Check it works.** `hermes plugins list` shows `jezero` as enabled. Ask "which Jezero
sources can you read?": the agent should call `jezero_list_sources`.

**Send older sessions.**

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py backfill --agent hermes
```

It reads `state.db` in `HERMES_HOME` (default `~/.hermes`); pass `--hermes-home FOLDER`
for another profile.

**Update.**

```bash
hermes plugins update jezero
```

Restart Hermes afterwards.

**Remove.**

```bash
hermes plugins remove jezero
```

## Antigravity

The Antigravity plugin brings the MCP server, the hooks and the skill.

1. Install it:

   ```bash
   agy plugin install https://github.com/MarsGotta/jezero/tree/main/plugins/antigravity
   ```

   > [!WARNING]
   > Use the full path to `plugins/antigravity`. The bare repository URL installs every
   > plugin folder of the repository under the same name, and the last one, Claude
   > Code's, wins: no MCP server and hooks that cannot run.

   The plugin lands in `~/.gemini/config/plugins/jezero`.

2. Allow the tools. Antigravity asks once before the first call of a read-only tool.
   Headless `agy` runs cannot ask, so add the rule to
   `~/.gemini/antigravity-cli/settings.json`:

   ```json
   {
     "permissions": {
       "allow": ["mcp(jezero_jezero/*)"]
     }
   }
   ```

The MCP server is a local bridge, `jezero_client.py mcp --agent antigravity`, that adds
the key to each request, so the key never sits in Antigravity's configuration. The bridge
and the hooks use `~/.gemini/config/jezero` when it holds a key, for sandboxes that see
`~/.gemini` but not your home (see [Agents in a sandbox](devices.md#agents-in-a-sandbox)).

**Check it works.**

```bash
agy plugin list
```

You should see:

```text
Installed plugins:
- jezero (enabled)
    Source:     antigravity
    Components: 1 skill, 1 MCP server, 1 hook
```

**Send older conversations.**

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py backfill --agent antigravity
```

**Update.** Run the install command again.

**Remove.**

```bash
agy plugin uninstall jezero
```

## Any other MCP client

Any client that speaks MCP over Streamable HTTP can read Jezero. It gets the tools only:
no hooks, no upload.

1. Give it a key of its own, so you can limit and revoke it separately. On the server:

   ```bash
   jezero new-token
   ```

   Hand the token to the client and add a client block with the digest, then restart the
   server:

   ```toml
   [[clients]]
   name = "shop-assistant"
   token_sha256 = "SHA256"
   sources = ["agent-*"]
   ```

   See [`[[clients]]`](../reference/configuration.md#clients) and [Limit what a key can
   read](devices.md#limit-what-a-key-can-read).

2. Configure the client with the URL `https://YOUR-JEZERO/mcp` and the header
   `Authorization: Bearer ***`. The field names depend on the client; a common shape is:

   ```json
   {
     "mcpServers": {
       "jezero": {
         "type": "http",
         "url": "https://YOUR-JEZERO/mcp",
         "headers": { "Authorization": "Bearer ***" }
       }
     }
   }
   ```

3. Add the protocol to the client's instructions, so it uses Jezero by habit:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py protocol
   ```

A client that only runs local (stdio) servers, or whose configuration you want to keep
free of keys, can use the bridge with this computer's key:

```json
{
  "mcpServers": {
    "jezero": {
      "command": "python3",
      "args": ["/home/me/.jezero-src/jezero-client/jezero_client.py", "mcp"]
    }
  }
}
```

## Use the app

The [Jezero app](app.md) shows the agents on this computer and the Jezero version in each:

```bash
jezero agents
```

Its Agents view (`4`) has an install or update button per agent, and shows the command
before running it. For Hermes Agent and Antigravity the button runs the plugin's install
command from this page. For Claude Code, Codex and OpenCode it runs
`jezero_client.py setup --agent NAME`, which registers the MCP server only: install the
Claude Code and Codex plugins with their own commands, and OpenCode with
`--opencode-plugin` as shown above.

## Next steps

- [Manage computers and keys](devices.md): read-only computers, sandboxes and the upload
  queue.
- [Troubleshooting](troubleshooting.md): when an agent does not use Jezero, or nothing
  arrives.
- [MCP tools](../reference/mcp-tools.md): what each tool takes and returns.
