# Connect a second computer

In this tutorial you connect a laptop to your Jezero server. The laptop gets its own key,
the Claude Code plugin sends its sessions to the server when they end, and you watch the
first one become an item you can search from either computer.

## Before you start

You need:

- A Jezero server you administer, reachable from the laptop at `https://YOUR-JEZERO`, with
  `require_auth = true`. [Run Jezero as a service](../how-to/deploy.md) sets one up,
  including [the address other computers
  use](../how-to/deploy.md#reach-it-from-other-computers).
  The server is a checkout of the repository, as in [Your first library](tutorial.md);
  server commands run in that folder (`~/jezero` below).
- An API key for the [summarizer](../glossary.md#summarizer) model, used in step 8.
- A laptop with `python3` (3.9 or newer), `curl` and
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Each step says where it runs: **on the server** or **on the laptop**.

## 1. Build the client bundle (on the server)

The laptop downloads the client and the plugins from your server. Build the bundle once
in the checkout:

```bash
cd ~/jezero
python3 scripts/build_clients.py
```

You should see:

```text
built /home/me/jezero/dist/client, /home/me/jezero/dist/jezero-client.tar.gz and plugins/ (version 0.4.0)
```

The server offers the file at `/client/jezero-client.tar.gz` without a key: it is the same
public code as the repository. See [Serve the plugin
bundle](../how-to/deploy.md#serve-the-plugin-bundle).

## 2. Get the client (on the laptop)

Download the bundle and unpack it in `~/.jezero-src`:

```bash
mkdir -p ~/.jezero-src
curl -fsSL https://YOUR-JEZERO/client/jezero-client.tar.gz | tar -xz -C ~/.jezero-src
python3 ~/.jezero-src/jezero-client/jezero_client.py --version
```

You should see:

```text
0.4.0
```

`jezero_client.py` is the [client core](../glossary.md#client-core): one Python file with
no dependencies, shared by every plugin.

## 3. Create the laptop's key (on the laptop)

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop
```

You should see:

```text
Device key created in /home/me/.jezero (the key itself never leaves this computer).
Ask the Jezero admin to approve it with:

  jezero device approve laptop SHA256
```

The key is in `~/.jezero/token`, readable only by you, and the server address in
`~/.jezero/client.json`. The printed `SHA256` is the key's fingerprint, not a secret:
send the whole `jezero device approve` line to the server's admin, which is you.

Check the key before it is approved:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py status
```

You should see:

```text
server https://YOUR-JEZERO: HTTP 401 {"ok":false,"error":"unknown token"}
queued sessions: 0
```

> [!NOTE]
> A computer whose sessions must not reach Jezero, such as the server itself (its
> connectors already read its sessions), gets a key with `init ... --no-upload`. The
> printed line then ends in `--read-only`, and the plugins there only search. If `init`
> says the computer is `already set up`, it already has a key (for example from the
> first tutorial): add `--force` to replace it.

## 4. Approve the laptop (on the server)

Run the line the laptop printed, with `uv run` in front:

```bash
cd ~/jezero
uv run jezero device approve laptop SHA256
```

You should see:

```text
approved laptop; restart the MCP server so it accepts the new key
```

The command appended to `jezero.toml` a [client](../glossary.md#client) for the laptop:

```toml
[[clients]]
# Device laptop: reads Jezero and uploads its sessions to the inbox.
name = "laptop"
token_sha256 = "SHA256"
sources = ["agent-*"]
upload = true
```

`sources = ["agent-*"]` lets the laptop read every agent source, its own included.
`upload = true` lets it send session files to its own folder of the
[inbox](../glossary.md#inbox), `data/inbox/laptop/`. The command also added five
[connectors](../glossary.md#connector), one per agent, that read that folder:
`agent-claude-code-laptop`, `agent-codex-laptop`, `agent-opencode-laptop`,
`agent-hermes-laptop` and `agent-antigravity-laptop`. They start with `enabled = false`.

The server reads its clients when it starts. Restart it, as a service:

```bash
systemctl --user restart jezero-serve.service
```

Or, if it runs in a terminal, stop it with `Ctrl+C` and start `uv run jezero serve` again.

## 5. Check the connection (on the laptop)

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py status
```

You should see:

```text
server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"0.1.0.dev0"}
queued sessions: 0
```

## 6. Set up Claude Code (on the laptop)

Register the Jezero MCP server in Claude Code, with the laptop's key:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py setup --agent claude-code
claude mcp list
```

You should see:

```text
claude: MCP registered
Checking MCP server health…

jezero: https://YOUR-JEZERO/mcp (HTTP) - ✔ Connected
```

`setup` registers the server for your user, in every folder. Then install the plugin, which
adds the hooks and the `jezero` skill:

```bash
claude plugin marketplace add MarsGotta/jezero
claude plugin install jezero@jezero
```

You should see:

```text
...
✔ Successfully added marketplace: jezero (declared in user settings)
Installing plugin "jezero@jezero"...✔ Successfully installed plugin: jezero@jezero (scope: user)
```

The plugin's [hooks](../glossary.md#hook) give every session a short
[protocol](../glossary.md#protocol) and recent work of its project, add related past work
to the first prompt, and upload the session's transcript when it ends. A hook never
blocks Claude Code: its network calls time out after a few seconds, and errors go to
`~/.jezero/client.log`.

## 7. Work one session (on the laptop)

Start Claude Code in a project, for example `~/projects/atlas`:

```bash
cd ~/projects/atlas
claude
```

Give it a real task, let it finish, and leave with `/exit`. When the session ends, the
plugin queues its transcript and uploads it in the background.

Check that it left the laptop:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py status
```

You should see `queued sessions: 0`. A session that could not be sent (offline, server
down) stays queued and goes out with the next one.

On the server, the transcript is now in the laptop's inbox, next to a `.project` file that
holds the project the laptop detected:

```bash
cd ~/jezero
find data/inbox/laptop/claude-code -type f
```

You should see:

```text
data/inbox/laptop/claude-code/projects/-home-me-projects-atlas/SESSION-ID.jsonl.project
data/inbox/laptop/claude-code/projects/-home-me-projects-atlas/SESSION-ID.jsonl
```

The server's log shows the upload as a `PUT /inbox/v1/claude-code/projects/...` request
with `200 OK`.

## 8. Turn the session into an item (on the server)

The inbox holds the raw transcript. A connector reads it, the summarizer writes the item,
and the library stores it. That last part calls a language model, so it needs the
summarizer.

1. Ask what a run would do, without calling the model:

   ```bash
   uv run jezero run --connector agent-claude-code-laptop --dry-run
   ```

   Right after the upload you should see `"deferred_sessions": 1`:

   ```json
   {"connector": "agent-claude-code-laptop", "docs": 0, "unchanged": 0, "stored": 0, "failed": 0, "deferred_sessions": 1, "redactions": 0, "input_chars": 0, "input_tokens": 0, "output_tokens": 0, "dry_run": true, "errors": [], "warnings": []}
   ```

   A connector waits until a session has been quiet for 30 minutes (the option
   `settle_minutes`), so it never summarises a session that is still growing. The clock
   starts at the upload. Come back after 30 minutes and run the same command. You should
   see the session counted as work:

   ```json
   {"connector": "agent-claude-code-laptop", "docs": 1, "unchanged": 0, "stored": 0, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 700, "input_tokens": 0, "output_tokens": 0, "dry_run": true, "errors": [], "warnings": []}
   ```

   `input_chars` is the size of what would go to the model, after cleaning; yours
   differs.

2. Give the server a summarizer, if it has none. Add to `jezero.toml`, with a small, fast
   model of your provider:

   ```toml
   [summarizer]
   provider = "anthropic"
   model = "MODEL"
   api_key_env = "JEZERO_SUMMARIZER_API_KEY"
   ```

   Put the key in a file named `.env` next to `jezero.toml` (git ignores it):

   ```text
   JEZERO_SUMMARIZER_API_KEY=YOUR-API-KEY
   ```

   Check that Jezero sees it:

   ```bash
   uv run jezero check-config
   ```

   You should see, at the end:

   ```text
   environment variables (values never shown):
     - JEZERO_SUMMARIZER_API_KEY: set
   ```

   Other providers and options are in
   [Configuration](../reference/configuration.md#summarizer).

3. Run a pilot on the newest sessions:

   ```bash
   uv run jezero run --connector agent-claude-code-laptop --sample 3
   ```

   You should see `"stored": 1`:

   ```json
   {"connector": "agent-claude-code-laptop", "docs": 1, "unchanged": 0, "stored": 1, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 700, "input_tokens": ..., "output_tokens": ..., "dry_run": false, "errors": [], "warnings": []}
   ```

   Without a summarizer, the run stops with
   `"error": "SummarizerError: summarizer.model is not set in the config"`.

4. The pilot does not move the connector's cursor. Turn the connector on so every run
   reads the laptop's new sessions: in `jezero.toml`, find the `[[connectors]]` block
   with `name = "agent-claude-code-laptop"` and change its `enabled = false` to
   `enabled = true`. Then run every enabled connector once:

   ```bash
   uv run jezero run
   ```

   You should see the session reported as `"unchanged": 1`: the pilot already stored it,
   and Jezero summarises a session again only when its content changes.

From now on, `jezero run --due` on a timer collects the laptop's sessions without you. See
[Run Jezero as a service](../how-to/deploy.md).

## 9. Search it (on both computers)

On the server, `jezero search` sees every source. Search with a few distinctive words of
your session:

```bash
uv run jezero search "WORDS"
```

You should see a line with the new source and the project:

```text
[4]   1.714  2026-10-21  agent-claude-code-laptop  atlas  TITLE
```

The title, summary and atoms are what the summarizer wrote; `jezero show 4` (or the app)
shows them.

On the laptop, Claude Code finds the item too. Start a new session in any folder and ask
about the work you did, for example *"What did we change in atlas today? Check Jezero."*
The agent calls `search` and cites the item id.

To search from the laptop's terminal, install the `jezero` command there. It reads with
the laptop's key, so it sees the same sources as its agents:

```bash
uv tool install git+https://github.com/MarsGotta/jezero
jezero find WORDS
```

You should see:

```text
 #4  TITLE
      ✻ atlas · 12 h ago

1 results · jezero show ID for the whole memory
```

If `jezero` is not found, add `~/.local/bin` to your `PATH`. The laptop reads only
`agent-*` sources: if the server holds the `demo` items of the first tutorial, they do not
appear here.

## What you built

- A laptop with its own key in `~/.jezero`, approved on the server as the client `laptop`.
- Claude Code on the laptop with the Jezero MCP server, the hooks and the skill.
- A connector, `agent-claude-code-laptop`, that turns the laptop's uploaded sessions into
  items.

## Next steps

- [Connect an agent](../how-to/connect-agents.md): Codex, OpenCode, Hermes Agent and Antigravity on
  the same laptop, and older sessions with `jezero_client.py backfill`.
- [Manage computers and keys](../how-to/devices.md): read-only computers, narrower
  sources, and removing a key.
- [Troubleshooting](../how-to/troubleshooting.md): when a session does not arrive or a key
  is refused.
- [Command reference](../reference/cli.md#jezero_clientpy-init): every command of the
  client core.
