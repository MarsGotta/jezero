# Client

The client core, `jezero_client.py`: where it keeps its files, what `client.json` holds,
the environment variables it reads, what each hook does for each agent, and how sessions
travel to the server. For the flags of each command, see
[`jezero_client.py`](cli.md#jezero_clientpy).

The core is one Python file (Python 3.9 or newer, standard library only, so it runs with
the `python3` that ships with macOS). Every plugin carries a copy in its `core/` folder,
next to `jezero_projects.py`, the project rules it shares with the server. Its version is
`VERSION` in the file (`0.4.0`). The plugins are thin adapters that map their agent's
events to the core's [hook events](#hook-events).

Nothing in the core may block or break an agent: every network call has a short timeout,
hooks catch their own errors and write them to `client.log`, and uploads run in a
detached process.

## Client home

The folder that holds this computer's key, queue and state. The core picks the first of:

1. `$JEZERO_CLIENT_HOME`, when set.
2. For commands run with `--agent antigravity` (`hook`, `mcp`) only:
   `~/.gemini/config/jezero`, when it holds a `client.json`.
3. `$CLAUDE_CONFIG_DIR/jezero`, when it holds a `client.json`.
4. `$CODEX_HOME/jezero`, when it holds a `client.json`.
5. `~/.jezero`.

Steps 2 to 4 are for agents run in a sandbox that cannot see your home folder: they keep
their own key inside the agent's config folder. To create one there, run `init` with
`JEZERO_CLIENT_HOME` pointing at that folder.

The OpenCode plugin picks its own home the same way, with
`$XDG_CONFIG_HOME/opencode/jezero` (default `~/.config/opencode/jezero`) as the agent
folder, and passes it to the core as `JEZERO_CLIENT_HOME`.

## Files

| Path in the client home | Written by | Content |
|---|---|---|
| `client.json` | `init` | The [settings](#clientjson). |
| `token` | `init` | The key, mode `0600`. It never leaves the computer except in `Authorization` headers. |
| `spool/pending/<agent>__<session>.json` | hooks, `backfill` | One [queued upload](#upload-queue) per session. |
| `spool/hermes/<session>.json` | Hermes hooks, `backfill --agent hermes` | [Hermes exports](#hermes-exports) waiting to be uploaded. |
| `spool/opencode/<session>.json` | the OpenCode plugin | OpenCode session exports waiting to be uploaded. |
| `spool/flush.lock` | `flush` | Held while an upload runs; considered stale after 10 minutes. |
| `state.json` | hooks, `flush` | What was uploaded, prompted and throttled ([below](#statejson)). |
| `client.log` | everything | One line per error or notable event, with a local timestamp. |
| `ca.pem` | macOS, on a certificate error | The system's root certificates, exported once (see [TLS](#tls)). |
| `core/jezero_client.py`, `core/jezero_projects.py` | `setup --agent opencode` | A stable copy of the core for the OpenCode plugin file. |

### client.json

```json
{
  "url": "https://YOUR-JEZERO",
  "device": "laptop",
  "projects": {
    "paths": {"~/clients/shop-web": "shop"},
    "roots": ["work"],
    "aliases": {"atlas-wt": "atlas"}
  }
}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `url` | string | required | Server base URL. `init` strips a trailing `/` and `/mcp`. The core always calls `<url>/mcp`, so a server with another `[server].path` does not work with the plugins. |
| `device` | string | set by `init` | This computer's name, `^[a-z0-9][a-z0-9_-]{0,63}$`, used in the approval command. |
| `upload` | boolean | `true` | `false` (written by `init --no-upload`) makes a [read-only computer](#read-only-computers). |
| `projects` | object | none | [Project rules](#projects) for this computer. |

The client counts as set up when `client.json` has a `url` and `token` is not empty. The
`device` value is informational: the server knows the device by the name it was approved
with.

### Projects

`projects` follows the same rules as the server's
[`[projects]`](configuration.md#projects), first match wins:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `paths` | object of string | `{}` | Folder → project. `~` matches any home folder (`/Users/<u>`, `/home/<u>`, `C:\Users\<u>`). Longest folder wins. |
| `git` | boolean | `true` | Use the name of the git repository the folder is in (the main repository for a worktree). |
| `roots` | list of string | `projects`, `code`, `dev`, `src`, `repos`, ... | Folder names that hold projects: the folder right below is the project. |
| `aliases` | object of string | `{}` | Rename a detected project. |

The client applies the rules to the session's working folder, so the git rule works
with this computer's repositories. The answer travels with the upload as
`X-Jezero-Project` and wins on the server (see
[HTTP API](http-api.md#upload-a-session-file)). The same project chooses the recent work
shown at session start.

### state.json

| Key | Type | Meaning |
|---|---|---|
| `uploaded` | object | Path of each uploaded file → `[mtime, size]` when it was sent. An unchanged file is not sent again. |
| `prompted` | object | Session id → time of its first prompt. The newest 500 are kept. |
| `stop_flush` | object | Session id → time the last `stop` started an upload. |
| `last_flush` | number | Time of the last `flush`, in seconds since the epoch. |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `JEZERO_CLIENT_HOME` | none | The [client home](#client-home), overriding every other rule. |
| `JEZERO_HOOK_TIMEOUT` | `2.5` | Seconds a hook waits for the server (context at session start and first prompt). |
| `JEZERO_CA_FILE` | none | PEM file of trusted certificates for the server, instead of the system's. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's folder: a sandboxed key, and the sessions `backfill` reads. |
| `CODEX_HOME` | `~/.codex` | Codex's folder: a sandboxed key, the sessions `backfill` reads, the `config.toml` `setup` edits. |
| `HERMES_HOME` | `~/.hermes` | Hermes profile whose `state.db` is exported. |
| `XDG_CONFIG_HOME` | `~/.config` | Where `setup` installs the OpenCode plugin, and where the plugin looks for a key. |
| `ANTIGRAVITY_CONVERSATION_ID` | none | Session id for Antigravity hooks whose input has no `conversationId`. |
| `JEZERO_CORE` | none | OpenCode plugin only: path of the core to run. |

## Commands

| Command | What it does |
|---|---|
| [`init`](cli.md#jezero_clientpy-init) | Creates the key and `client.json`, prints `jezero device approve DEVICE SHA256` for the server's admin. Refuses if already set up, unless `--force`. |
| [`status`](cli.md#jezero_clientpy-status) | Checks the key with the server and shows the queue. Exit 0 when the server accepts the key. |
| [`setup`](cli.md#jezero_clientpy-setup) | Registers the MCP server in the agents ([below](#setup)). |
| [`mcp`](cli.md#jezero_clientpy-mcp) | Runs the [stdio MCP bridge](#mcp-bridge). |
| [`hook`](cli.md#jezero_clientpy-hook) | Runs one [hook event](#hook-events), reading the agent's JSON on stdin. Always exits 0. |
| [`flush`](cli.md#jezero_clientpy-flush) | Uploads the queue now. Prints the counts only on a terminal. |
| [`backfill`](cli.md#jezero_clientpy-backfill) | Queues sessions already on this computer ([below](#backfill)). |
| [`protocol`](cli.md#jezero_clientpy-protocol) | Prints the [protocol](#protocol). |

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop
```

You should see:

```text
Device key created in /home/me/.jezero (the key itself never leaves this computer).
Ask the Jezero admin to approve it with:

  jezero device approve laptop SHA256
```

`status` prints, on a computer that uploads:

```text
server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"0.1.0.dev0"}
queued sessions: 0
```

and on a [read-only computer](#read-only-computers), after a `list_sources` call:

```text
server https://YOUR-JEZERO: read-only, 4 readable sources
```

## Hook events

| Event | What the core does |
|---|---|
| `session-start` | Prints the [protocol](#protocol). When set up, it also starts a background upload of anything still queued (not on a read-only computer), and adds the project's recent work: a `timeline` call for the project of the session's folder, at most 5 items. Not set up: the protocol only. |
| `user-prompt` | Only the first prompt of each session. Takes the prompt's distinctive words; with at least two, runs `search` with them (limit 8) and adds up to 3 results in which at least two of the words appear as whole words in the title or summary. |
| `stop` | Queues the session's transcript and starts a background upload, at most once every 15 minutes per session. |
| `session-end` | Queues the transcript and starts a background upload, without the throttle. |

`stop` and `session-end` do nothing on a [read-only computer](#read-only-computers) or
when the transcript is not a file the server accepts (for example a Claude Code
sub-agent transcript).

The text the core adds looks like this:

```text
Recent work on project `billing` in Jezero (newest first):
- [3] 2026-10-20 Dashboard chart colours
- [2] 2026-10-19 Invoice export: back to plain UTF-8 for the accounting import
```

```text
Jezero has past work that may be related (check with `get_item` before using):
- [2] 2026-10-19 Invoice export: back to plain UTF-8 for the accounting import
```

The prompt's words are lowercased, without accents, at least 3 characters (letters,
digits, `.`, `-`, `_`), without English and Spanish stop words, unique, at most 20.

The hook reads these fields of the agent's JSON: `session_id`, `cwd`, `prompt`
(`user-prompt`), `transcript_path` (`stop`, `session-end`) and `hermes_home` (Hermes).

### Per agent

| Agent | Agent event | Core event | Notes |
|---|---|---|---|
| Claude Code | `SessionStart` (`startup`, `resume`, `clear`, `compact`) | `session-start` | Timeout 10 s. Output becomes context. |
| | `UserPromptSubmit` | `user-prompt` | Timeout 10 s. |
| | `Stop` | `stop` | Asynchronous, timeout 10 s. |
| | `SessionEnd` | `session-end` | Timeout 5 s. |
| Codex | `SessionStart` (`startup`, `resume`, `clear`, `compact`) | `session-start` | Timeout 10 s, status "Loading Jezero context...". |
| | `UserPromptSubmit` | `user-prompt` | Timeout 5 s. |
| | `Stop` | `stop` | Timeout 5 s. Prints `{}` when there is nothing to say: Codex expects JSON. |
| | `SessionEnd` | `session-end` | Timeout 3 s. |
| OpenCode | first `experimental.chat.system.transform` of a session | `session-start` | The text joins the system prompt, every turn of the session. |
| | first `chat.message` of a session | `user-prompt` | The prompt's text parts; the result also joins the system prompt. |
| | `session.idle` event | `session-end` | Exports the session through the SDK to `spool/opencode/<id>.json`, then runs the hook detached. |
| | `config` | | Registers the MCP server `jezero` (`<url>/mcp` with the key) unless one is already configured. |
| Hermes Agent | `pre_llm_call`, first turn | (core functions) | Recent work and related past work, added to the user's message. |
| | `on_session_end` | `session-end` | At most every 10 minutes per session, in a background thread. |
| | `on_session_finalize` | `session-end` | Always. |
| Antigravity | `SessionStart` | `session-start` | Timeout 15 s. |
| | `PreInvocation` | `user-prompt` | Timeout 15 s. Only the first model call of a request (`invocationNum` 0 or absent); the prompt is the last `<USER_REQUEST>` of the transcript. |
| | `Stop` | `stop` | Timeout 10 s. Ignored while `fullyIdle` is `false`. |

The OpenCode plugin runs `session-start` and `user-prompt` with a 5-second limit.

#### Hermes Agent

The Hermes plugin imports the core instead of running it. It adds the `jezero_search`,
`jezero_get_item`, `jezero_later`, `jezero_timeline` and `jezero_list_sources` tools
(see [MCP tools](mcp-tools.md#in-hermes-agent-and-antigravity)), each with a 20-second
timeout, and only once the computer is set up. It does not add the protocol: its tool
descriptions and the `jezero:jezero` skill carry the same advice. The first-turn context
starts with `Jezero (shared memory of past work; check with jezero_get_item and
jezero_later before relying on it):`.

#### Antigravity

Antigravity hooks read and write JSON. The core takes `conversationId` as the session id,
the first of `workspacePaths` as the folder and `transcriptPath` as the transcript, and
answers `{}` or, with context:

```json
{"injectSteps": [{"systemMessage": {"systemMessage": "## Jezero: shared memory of past work (read-only) ..."}}]}
```

## Protocol

The text `session-start` gives every agent, also printed by `protocol`:

```text
## Jezero: shared memory of past work (read-only)

Jezero indexes past sessions of all of this user's agents and their project tasks: decisions, fixes, exact errors, commands and facts, each with the date it happened. Use it out of habit, without waiting to be asked:

- Before non-trivial work in a project, or when the user mentions something you have no context on, call the jezero `search` tool with 2-5 distinctive words (names, errors, ids). Matching is literal: try a synonym if nothing comes back.
- Before relying on a decision, plan, status or number from Jezero, call `later` on that item: a later session may have changed it. Answer with the newest state.
- Cite item ids like [123] when an answer comes from Jezero. If Jezero has nothing, say so; never fill the gap with a guess.
- Jezero is read-only and ingests sessions by itself: there is nothing to save.
```

## Upload queue

1. **Queue.** `stop`, `session-end` and `backfill` check that the transcript maps to an
   inbox path (table below) and write a pointer,
   `spool/pending/<agent>__<session>.json`:
   `{"agent", "session_id", "path", "cwd", "project", "queued_at"}`. The folder is the
   hook's `cwd`, else the first `cwd` found in the first 50 lines of the transcript (or
   the `cwd` of a Hermes export); the project comes from [the rules](#projects). A newer
   pointer for the same session replaces the older one.
2. **Start.** The hook starts `python3 jezero_client.py flush` as a detached process, so
   the agent never waits for the network.
3. **Flush.** One flush at a time (`spool/flush.lock`). Pointers are taken in name order:
   - an unreadable pointer is deleted;
   - a transcript that no longer exists counts as `gone`;
   - a transcript with the same modification time and size as when it was last sent counts
     as `unchanged`;
   - the rest are sent gzip-compressed with
     [`PUT /inbox/v1/<agent>/<path>`](http-api.md#upload-a-session-file) and
     `X-Jezero-Project`, with a 60-second timeout, and count as `sent`.
4. **Answer.** HTTP 200: sent. HTTP 400, 403, 413 or 415: the server will never accept the
   file; it is logged and dropped. Any other status or a network error: the flush stops
   (`failed`) and the queue stays for the next hook.

`flush` on a terminal prints the counts:

```json
{"sent": 1, "unchanged": 0, "gone": 0, "failed": 0}
```

| Agent | Transcript accepted | Inbox path |
|---|---|---|
| `claude-code` | `*.jsonl`, not under `subagents/` | `projects/<parent folder>/<file>` |
| `codex` | `rollout-*.jsonl` or `rollout-*.jsonl.zst` | `sessions/YYYY/MM/DD/<file>`, from the folders or from the date in the name |
| `opencode`, `hermes` | `*.json` | `sessions/<name>.json` |
| `antigravity` | `brain/<conversation>/.../transcript.jsonl` | `brain/<conversation>/transcript.jsonl` |

Characters other than `A-Z a-z 0-9 . _ -` become `_`, and each part is cut at 150
characters.

### Hermes exports

Hermes keeps its sessions in a database, so the core writes one file per session to
`spool/hermes/<session>.json`, the shape the server's `hermes` connector reads with
`export_dir`:

```json
{
  "format": "jezero-hermes-export/1",
  "session": {"id": "20261021_091200_ab12cd", "source": "cli", "title": "Invoice export fix", "started_at": 1792573920.0, "cwd": "/home/me/projects/billing"},
  "messages": [
    {"id": 1, "role": "user", "content": "the invoice export breaks accents", "tool_calls": null, "timestamp": 1792573921.5}
  ]
}
```

Only user and assistant messages are exported (active ones, when the database marks
them), and only the session columns that exist. Sessions whose source is `cron` or
`subagent` are not exported. The file is rewritten only when the session changed, so an
unchanged session is not uploaded again.

### Backfill

`backfill` queues the sessions changed in the last `--days` (default 90) days, then
starts an upload.

| `--agent` | Reads |
|---|---|
| `claude-code` (default) | `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`, default `~/.claude/projects` |
| `codex` (default) | `$CODEX_HOME/sessions/*/*/*/rollout-*.jsonl*`, default `~/.codex/sessions` |
| `hermes` | Sessions of `$HERMES_HOME/state.db` (or `--hermes-home`) whose last message is recent enough, except `cron` and `subagent` |
| `antigravity` | `~/.gemini/{antigravity-cli,antigravity,antigravity-ide}/brain/*/.system_generated/logs/transcript.jsonl` |
| `opencode` | Not supported: sessions arrive through the plugin. |

You should see:

```text
queued 12 sessions; uploading in the background
```

## Read-only computers

`init --no-upload` writes `"upload": false`. Then hooks give context but never queue or
upload, `backfill` prints `read-only computer (init --no-upload): nothing to upload`, and
`status` checks the key with `list_sources` instead of the inbox. Use it on the computer
that runs the server, whose sessions the connectors already read, and approve it on the
server with `jezero device approve DEVICE SHA256 --read-only`.

## MCP bridge

`jezero_client.py mcp` is a local stdio MCP server that forwards every JSON-RPC line it
reads to `<url>/mcp` with this computer's key, so the agent's configuration holds no key.
The Antigravity plugin uses it. It keeps the server's `Mcp-Session-Id`, waits up to 60
seconds per message, and answers requests (not notifications) with a JSON-RPC error
`-32000` when it cannot help:

| Situation | Error message |
|---|---|
| Not set up | ``Jezero is not connected on this computer: run `jezero_client.py init`.`` |
| HTTP error or no connection | `Jezero is not reachable right now (HTTP STATUS): DETAIL` |

## Setup

`setup` registers the MCP server `jezero` in each agent found on the computer (all of
them without `--agent`):

| Agent | What `setup` does |
|---|---|
| Claude Code | `claude mcp add --scope user --transport http jezero <url>/mcp --header "Authorization: Bearer ***"`, unless `claude mcp get jezero` already finds one. |
| Codex | Appends `[mcp_servers.jezero]` with `url` and `http_headers` to `$CODEX_HOME/config.toml`, unless that table exists. |
| OpenCode | With `--opencode-plugin PATH`, installs the plugin as `$XDG_CONFIG_HOME/opencode/plugins/jezero.ts` (pointing at a copy of the core in the client home) and the `jezero` skill. The plugin registers the MCP server itself. |
| Hermes Agent | Prints the `hermes plugins install` command: the plugin brings its own tools. |
| Antigravity | Prints the `agy plugin install` command: the plugin brings the MCP bridge, the skill and the hooks. |

> [!WARNING]
> For Claude Code and Codex, `setup` writes the key into the agent's own configuration
> (Claude Code's user-scope MCP settings, Codex's `config.toml`). Treat those files as
> secrets.

## TLS

The core verifies the server's certificate with `$JEZERO_CA_FILE` when it is set, else
with `ca.pem` in the client home when it exists, else with Python's default trust store.
On macOS, where Python may not trust the system's certificates, a certificate error makes
the core export the keychains' root certificates to `ca.pem` once and retry the request.

## Limits

| Limit | Value |
|---|---|
| Hook wait for the server | `JEZERO_HOOK_TIMEOUT`, 2.5 s |
| Recent items at session start | 5 |
| Related items on the first prompt | 3, out of a search of 8 |
| Prompt words | at least 2, at most 20 |
| Sessions remembered as prompted | 500 |
| Upload throttle on `stop` | once per 15 minutes per session |
| Hermes upload throttle (`on_session_end`) | once per 10 minutes per session |
| Upload request | 60 s |
| `status` request | 10 s |
| MCP bridge request | 60 s |
| Hermes tool request | 20 s |
| OpenCode `session-start` and `user-prompt` | 5 s |
| Stale upload lock | 10 minutes |
| Transcript lines scanned for `cwd` | 50 |

## See also

- [Connect a computer](../getting-started/connect-a-computer.md): the client, step by step.
- [Connect an agent](../how-to/connect-agents.md): installing each plugin.
- [HTTP API](http-api.md): the routes the client calls.
- [Command reference](cli.md#jezero_clientpy): every flag of `jezero_client.py`.
