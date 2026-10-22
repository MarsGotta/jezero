# Client

The client core, `jezero_client.py`: where it keeps its files, what `client.json` holds,
the environment variables it reads, what each hook does for each agent, and how sessions
travel to the server. For the flags of each command, see
[`jezero_client.py`](cli.md#jezero_clientpy).

The core is one Python file (Python 3.9 or newer, standard library only, so it runs with
the `python3` that ships with macOS). Every plugin carries a copy in its `core/` folder,
next to `jezero_projects.py`, the project rules it shares with the server. Its version is
`VERSION` in the file (`0.5.2`). The plugins are thin adapters that map their agent's
events to the core's [hook events](#hook-events).

Nothing in the core may block or break an agent: every network call has a short timeout,
hooks catch their own errors and write them to `client.log`, and uploads run in a
detached process.

## Client home

The folder that holds this computer's key, queue and state. The core picks the first of:

1. `$JEZERO_CLIENT_HOME`, when set.
2. For a command run with `--agent` (`hook`, `mcp`, `status`, `flush`, and each agent of
   `setup` and `backfill`), that agent's own folder, when it holds a `client.json`:
   `~/.gemini/config/jezero` for `antigravity`, `$CLAUDE_CONFIG_DIR/jezero` for
   `claude-code`, `$CODEX_HOME/jezero` for `codex`.
3. `$CLAUDE_CONFIG_DIR/jezero`, then `$CODEX_HOME/jezero`, when it holds a `client.json`.
4. `~/.jezero`.

Steps 2 and 3 are for agents run in a sandbox that cannot see your home folder: they keep
their own key inside the agent's config folder. To create one there, run `init` with
`JEZERO_CLIENT_HOME` pointing at that folder. A background upload started by a hook runs
with `JEZERO_CLIENT_HOME` set to the hook's home, so it sends the queue of the key that
queued it.

The OpenCode plugin picks its own home the same way, with
`$XDG_CONFIG_HOME/opencode/jezero` (default `~/.config/opencode/jezero`) as the agent
folder, and passes it to the core as `JEZERO_CLIENT_HOME`.

## Files

The client home and its `spool` folder are private to your user (mode `0700`), and every
file the core writes there is too (mode `0600`): the spool holds copies of your sessions.
Files are written to a temporary name and renamed, so a crash never leaves half a file.

| Path in the client home | Written by | Content |
|---|---|---|
| `client.json` | `init` | The [settings](#clientjson). |
| `token` | `init` | The key, mode `0600`. Sent only in the `Authorization` header of the core's own requests; the server keeps its SHA-256, never the key. |
| `spool/pending/<agent>__<session>.json` | hooks, `backfill` | One [queued upload](#upload-queue) per session. |
| `spool/hermes/<session>.json` | Hermes hooks, `backfill --agent hermes` | [Hermes exports](#hermes-exports) waiting to be uploaded; deleted once uploaded. |
| `spool/opencode/<session>.json` | the OpenCode plugin | OpenCode session exports waiting to be uploaded; deleted once uploaded. |
| `spool/flush.lock` | `flush` | Held while an upload runs; considered stale after 10 minutes. |
| `spool/.upload.gz.*.tmp` | `flush` | The file being uploaded, compressed; deleted when the request ends. |
| `state.json` | hooks, `flush` | What was uploaded, rejected, prompted and throttled ([below](#statejson)). |
| `state.json.lock` | hooks, `flush` | Locked while `state.json` changes, so two writers never lose each other's changes. |
| `client.log` | everything | One line per error or notable event, with a local timestamp. |
| `ca.pem` | macOS, on a certificate error | The system's root certificates, exported once (see [TLS](#tls)). |
| `core/jezero_client.py`, `core/jezero_projects.py` | `setup` | A stable copy of the core: the MCP bridge that `setup` registers in Claude Code and Codex, and the OpenCode plugin file, run it from here. |

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
| `url` | string | required | Server base URL. `init` strips a trailing `/`, and the MCP path (`--mcp-path`, or `/mcp`) when the URL ends with it. |
| `mcp_path` | string | `/mcp` | URL path of the MCP endpoint, the server's `[server].path`. Written by `init --mcp-path PATH` only when it is not `/mcp`. The core, the bridge and the OpenCode plugin call `<url><mcp_path>`. |
| `device` | string | set by `init` | This computer's name, `^[a-z0-9][a-z0-9_-]{0,63}$`, used in the approval command. |
| `upload` | boolean | `true` | `false` (written by `init --no-upload`) makes a [read-only computer](#read-only-computers). |
| `insecure_http` | boolean | `false` | `true` (written by `init --insecure-http`) accepts a plain `http://` URL for another computer. Without it, `init` refuses one and `status` warns (see [HTTP](#http)). |
| `max_upload_mb` | number | `64` | The biggest file the client uploads, in MB. Raise it only when the server's [`[inbox].max_mb`](configuration.md) allows more. |
| `projects` | object | none | [Project rules](#projects) for this computer. |

The client counts as set up when `client.json` has a `url` and `token` is not empty. The
`device` value is informational: the server knows the device by the name it was approved
with.

> [!NOTE]
> `init --force` makes a new key and rewrites `url`, `device`, `mcp_path` and `upload`
> from the options you pass (`--no-upload`, `--mcp-path`); other settings such as
> `projects` are kept. For the same device name it prints the `jezero device rotate` line
> for the admin instead of `device approve`.

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
[HTTP API](http-api.md#upload-a-session-file)). The server only accepts names of ASCII
letters, digits, `.`, `_` and `-` (up to 64 characters); for any other name the client
leaves the header out, writes a line to `client.log`, and the server finds the project
itself. The same project chooses the recent work
shown at session start.

### state.json

| Key | Type | Meaning |
|---|---|---|
| `uploaded` | object | Path of each uploaded file → `[mtime, size]` when it was sent. An unchanged file is not sent again. The newest 2000 are kept. |
| `rejected` | object | Path of each file the server refused for good (HTTP 400, 413 or 415) → `{agent, mark, reason, at}`. Not sent again until the file changes. The newest 500 are kept. |
| `refused` | object | `{reason, at}` of the first HTTP 401 or 403 in a row: the server refused the key itself, so the whole queue waits. Cleared by the next upload that succeeds. |
| `exported` | object | Path of each uploaded export (OpenCode, Hermes) → SHA-256 of its content. The same session exported again is not uploaded again. The newest 2000 are kept. |
| `attempts` | object | Path of a large file whose upload was cut off → `{mark, count, at}` ([below](#upload-queue)). The newest 500 are kept. |
| `prompted` | object | Session id → time of its first prompt. The newest 500 are kept. |
| `stop_flush` | object | Session id → time the last `stop` started an upload. The newest 500 are kept. |
| `last_flush` | number | Time of the last `flush`, in seconds since the epoch. |

Hooks, the uploader and the Hermes plugin's threads write `state.json` at the same time,
so each change takes the lock on `state.json.lock` (and a lock inside the process),
reads the file again, applies itself and saves it. Where the system offers no file lock,
only the lock inside the process applies.

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
| [`init`](cli.md#jezero_clientpy-init) | Creates the key and `client.json`, prints `jezero device approve DEVICE SHA256` for the server's admin. Refuses if already set up, unless `--force`, and refuses a plain `http://` URL for another computer, unless `--insecure-http` (see [HTTP](#http)). |
| [`status`](cli.md#jezero_clientpy-status) | Shows the client home, checks the key with the server and shows the queue and the rejected uploads. `--agent NAME` uses that agent's key. Exit 0 when the server accepts the key. |
| [`setup`](cli.md#jezero_clientpy-setup) | Registers Jezero in the agents ([below](#setup)). |
| [`mcp`](cli.md#jezero_clientpy-mcp) | Runs the [stdio MCP bridge](#mcp-bridge). |
| [`hook`](cli.md#jezero_clientpy-hook) | Runs one [hook event](#hook-events), reading the agent's JSON on stdin (input that is not a JSON object counts as `{}`). Always exits 0. |
| [`flush`](cli.md#jezero_clientpy-flush) | Uploads the queue now and prints the counts. `--agent NAME` uses the queue of that agent's key. |
| [`backfill`](cli.md#jezero_clientpy-backfill) | Queues sessions already on this computer ([below](#backfill)). |
| [`protocol`](cli.md#jezero_clientpy-protocol) | Prints the [protocol](#protocol). |

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py init --url https://YOUR-JEZERO --device laptop
```

You should see:

```text
Device key created. The key stays in /home/me/.jezero; the server only keeps its SHA-256 fingerprint.
Ask the Jezero admin to approve it with:

  jezero device approve laptop SHA256
```

`status` prints, on a computer that uploads:

```text
key: /home/me/.jezero
server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"0.1.0.dev0"}
queued sessions: 0
rejected uploads: 0
```

While the server refuses the key (HTTP 401 or 403) and sessions wait, a line says since
when:

```text
key: /home/me/.jezero
server https://YOUR-JEZERO: HTTP 403 {"ok":false,"error":"this key may not upload"}
queued sessions: 1
uploads refused since 2026-10-22 03:32: HTTP 403 {"ok":false,"error":"this key may not upload"} (the queue is kept)
rejected uploads: 0
```

Files the server rejected for good are counted, and the five newest are listed with their
reason:

```text
key: /home/me/.jezero
server https://YOUR-JEZERO: HTTP 200 {"ok":true,"device":"laptop","version":"0.1.0.dev0"}
queued sessions: 0
rejected uploads: 1
  2026-10-22 03:35 claude-code: HTTP 413 {"ok":false,"error":"file too large"}
```

On a [read-only computer](#read-only-computers), after a `list_sources` call:

```text
key: /home/me/.jezero
server https://YOUR-JEZERO: read-only, 4 readable sources
```

Not set up, `status` exits 1 with ``not set up (/home/me/.jezero): run `init` first``.
With a plain `http://` URL for another computer and no `insecure_http`, its second line
is `warning: http://... is plain HTTP: the key would travel in clear. ...`.

## Hook events

| Event | What the core does |
|---|---|
| `session-start` | Not set up: prints nothing (the agent has no Jezero tools to use). Set up: prints the [protocol](#protocol), starts a background upload of anything still queued (not on a read-only computer), and adds the project's recent work: a `timeline` call for the project of the session's folder, at most 5 items. |
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
| Claude Code | plugin's `.mcp.json` | | Starts the [MCP bridge](#mcp-bridge), `python3 ${CLAUDE_PLUGIN_ROOT}/core/jezero_client.py mcp --agent claude-code`, as the MCP server `jezero`. |
| | `SessionStart` (`startup`, `resume`, `clear`, `compact`) | `session-start` | Timeout 10 s. Output becomes context. |
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
| | `config` | | Registers the MCP server `jezero` (`<url><mcp_path>` with the key) unless one is already configured. |
| Hermes Agent | `pre_llm_call`, first turn | (core functions) | The protocol (once per session), recent work and related past work, added to the user's message. Nothing without a key. |
| | `on_session_end` | `session-end` | At most every 10 minutes per session, in a background thread. |
| | `on_session_finalize` | `session-end` | Always. |
| Antigravity | `SessionStart` | `session-start` | Timeout 15 s. |
| | `PreInvocation` | `user-prompt` | Timeout 15 s. Only the first model call of a request (`invocationNum` 0 or absent); the prompt is the last `<USER_REQUEST>` of the transcript. |
| | `Stop` | `stop` | Timeout 10 s. Ignored while `fullyIdle` is `false`. |

The OpenCode plugin runs `session-start` and `user-prompt` with a 5-second limit.

#### Hermes Agent

The Hermes plugin imports the core instead of running it. It adds the `jezero_search`,
`jezero_get_item`, `jezero_later`, `jezero_timeline` and `jezero_list_sources` tools
(see [MCP tools](mcp-tools.md#in-hermes-agent-claude-code-codex-and-antigravity)), each
with a 20-second timeout, and only once the computer is set up. On the first turn of a
session it adds the [protocol](#protocol), once per session, with the tool names Hermes
uses (`jezero_search`, `jezero_later`, `jezero_get_item`); a session id that comes back
after a context compression does not get it twice. Recent and related work follow under
`Jezero (shared memory of past work; check with jezero_get_item and jezero_later before
relying on it):`. Without a key it adds nothing.

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
   inbox path (table below) and that it is [one of the agent's own
   files](#which-files-are-uploaded), and write a pointer,
   `spool/pending/<agent>__<session>.json`:
   `{"agent", "session_id", "path", "roots", "cwd", "project", "queued_at"}`. The folder is the
   hook's `cwd`, else the first `cwd` found in the first 50 lines of the transcript (or
   the `cwd` of a Hermes export); the project comes from [the rules](#projects). A newer
   pointer for the same session replaces the older one.
2. **Start.** The hook starts `python3 jezero_client.py flush` as a detached process, so
   the agent never waits for the network.
3. **Flush.** One flush at a time (`spool/flush.lock`). Pointers are taken in name order:
   - an unreadable pointer is deleted;
   - a transcript that no longer exists counts as `gone`;
   - a transcript with the same modification time and size as when it was last sent, or
     as when the server last rejected it, counts as `unchanged`, and so does an export
     whose content is the same as the last one uploaded;
   - a file that is no longer one of the agent's own files (for example replaced by a
     symbolic link since it was queued), or bigger than `max_upload_mb` (64 MB unless
     set), counts as `rejected` without being read or sent;
   - the rest are compressed with gzip into a temporary file in the spool (never whole in
     memory) and sent with
     [`PUT /inbox/v1/<agent>/<path>`](http-api.md#upload-a-session-file) (each part of the
     path percent-encoded) and `X-Jezero-Project`, with a 60-second timeout, and count as
     `sent`. An export (OpenCode, Hermes) is deleted from the spool once sent.
4. **Answer.**
   - HTTP 200: sent.
   - HTTP 401 or 403: the server refused the key (not approved yet, revoked, or not
     allowed to upload). The flush stops (`failed`), the whole queue stays, and
     `client.log` says so once: `uploads refused by the server (...): the queue stays
     until the server's admin allows this key to upload (upload = true in its
     [[clients]] block, picked up within a second)`. `status` shows `uploads refused since ...`.
   - HTTP 400, 413 or 415: the server will never accept this file as it is. It counts as
     `rejected`, leaves the queue, and is recorded in `state.json`; it is sent again only
     if the file changes. `status` lists it.
   - Any other status or a network error: the flush stops (`failed`) and the queue stays
     for the next hook.
   - The connection cut off while sending a body of more than 1 MB: a server may refuse a
     big file (HTTP 413) before reading it, which the client only sees as a reset. The
     flush stops (`failed`) and `state.json` counts the attempt under `attempts`; the
     third one for the same file makes it `rejected` (`connection reset 3 times while
     sending ...`), so one file cannot block the queue forever. A refused connection (the
     server is down) never counts.
   - A redirect: the flush stops (`failed`); see [HTTP](#http).
   - Any other error while preparing or sending one file (for example a name the URL
     cannot carry): that file counts as `rejected` with the error as its reason, and the
     flush goes on with the next one.

`flush` prints the counts:

```json
{"sent": 1, "unchanged": 0, "gone": 0, "failed": 0, "rejected": 0}
```

| Agent | Transcript accepted | Inbox path |
|---|---|---|
| `claude-code` | `*.jsonl`, not under `subagents/` | `projects/<parent folder>/<file>` |
| `codex` | `rollout-*.jsonl` or `rollout-*.jsonl.zst` | `sessions/YYYY/MM/DD/<file>`, from the folders or from the date in the name |
| `opencode`, `hermes` | `*.json` | `sessions/<name>.json` |
| `antigravity` | `brain/<conversation>/.../transcript.jsonl` | `brain/<conversation>/transcript.jsonl` |

Characters other than `A-Z a-z 0-9 . _ -` become `_` (Codex file names also keep `:`),
and each part is cut at 150 characters (200 for a Codex file name).

### Which files are uploaded

Only the agent's own session files. The transcript must be a regular file, not a
symbolic link, and its real location must be inside one of these folders (the folders
may themselves be reached through a symbolic link, as in a dotfiles setup):

| Agent | Folders |
|---|---|
| `claude-code` | `$CLAUDE_CONFIG_DIR/projects`, default `~/.claude/projects` |
| `codex` | `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, default `~/.codex` |
| `opencode`, `hermes` | `spool/opencode` or `spool/hermes` in the client home |
| `antigravity` | `~/.gemini/{antigravity-cli,antigravity,antigravity-ide}/brain` |

Anything else, such as a `transcript_path` in a hook's input that names another file, is
not queued, and `client.log` says why: `not queued for claude-code: PATH (it is a symbolic
link)`. The Antigravity `user-prompt` hook reads the prompt only from a transcript in
those folders too.

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
`subagent` are not exported. The file is rewritten only when the session changed, and
deleted once uploaded; the SHA-256 kept in `state.json` (`exported`) means an unchanged
session is not exported or uploaded again.

### Backfill

`backfill` queues the sessions changed in the last `--days` (default 90) days, then
starts an upload. Each agent's sessions go to the queue of the key that agent uses (see
[Client home](#client-home)), so a sandboxed agent's sessions upload under its own device
name.

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

An agent whose key is missing prints ``claude-code: not set up (/home/me/.jezero): run
`init` first``, and the other agents go on.

## Read-only computers

`init --no-upload` writes `"upload": false`. Then hooks give context but never queue or
upload, `backfill` prints `claude-code: read-only computer (init --no-upload): nothing to
upload` for each agent, and `status` checks the key with `list_sources` instead of the
inbox. Use it on the computer that runs the server, whose sessions the connectors already
read, and approve it on the server with `jezero device approve DEVICE SHA256 --read-only`.

## MCP bridge

`jezero_client.py mcp` is a local stdio MCP server that forwards every JSON-RPC line it
reads to `<url><mcp_path>` with this computer's key, so the agent's configuration holds no
key. The Claude Code and Antigravity plugins start it, and `setup` registers it in Claude
Code (when the plugin is not installed) and Codex. With `--agent NAME` it uses that
agent's key (see [Client home](#client-home)). It keeps the server's `Mcp-Session-Id`
and waits up to 60 seconds per message.

Every request (a message with an `id`) gets exactly one answer, and only JSON-RPC ever
reaches the agent. The server's replies (plain JSON or server-sent events, also with an
HTTP error status) are passed on when they are JSON-RPC; a request the server did not
answer gets a JSON-RPC error `-32000`. Notifications get no answer.

| Situation | Code | Error message |
|---|---|---|
| Not set up | -32000 | ``Jezero is not connected on this computer (no key in /home/me/.jezero): run `jezero_client.py init`.`` |
| No connection, or a [redirect](#http) | -32000 | `Jezero is not reachable right now: DETAIL` |
| HTTP 401 or 403 | -32000 | ``Jezero refused this computer's key (HTTP STATUS): DETAIL. Ask the Jezero admin to approve it (`jezero device approve`).`` |
| HTTP 500 or more | -32000 | `Jezero is not working right now (HTTP STATUS): DETAIL` |
| Another HTTP error | -32000 | `Jezero answered HTTP STATUS: DETAIL` |
| An answer that is not JSON-RPC (an HTML page, for example) | -32000 | `Jezero sent an answer that is not JSON-RPC (HTTP STATUS): DETAIL` |
| An empty answer to a request | -32000 | `Jezero sent no answer (HTTP STATUS)` |
| A line that is not JSON | -32700 | `Parse error: the line is not JSON` (with `id` null) |
| A batch (a JSON array), which MCP no longer has | -32600 | `Invalid request: JSON-RPC batches are not supported by MCP; send one by one`, once per request in it |
| JSON that is not an object | -32600 | `Invalid request: not a JSON object` (with `id` null) |

## Setup

`setup` registers Jezero in each agent named with `--agent` (every agent without it),
using the key that agent uses. An agent without a key prints ``NAME: not set up
(/home/me/.jezero): run `init` first``, and `setup` exits 1 after trying the others.

| Agent | What `setup` does |
|---|---|
| Claude Code | With the Jezero plugin installed (`jezero@` in `plugins/installed_plugins.json` of `CLAUDE_CONFIG_DIR`, default `~/.claude`): registers nothing, since the plugin brings the bridge, and removes a user-scope `jezero` entry left by an older `setup`. Without the plugin: registers the bridge at user scope, `claude mcp add --scope user jezero -- python3 <client home>/core/jezero_client.py mcp --agent claude-code` (plus `-e JEZERO_CLIENT_HOME=...` when that variable is set), replacing an old HTTP entry that held the key in clear. After removing an old HTTP entry it says that copies of the config may still hold the key (Claude Code keeps some in `CLAUDE_CONFIG_DIR/backups`) and how to rotate it. |
| Codex | Writes the bridge as `[mcp_servers.jezero]` in `$CODEX_HOME/config.toml` (default `~/.codex`): `command = "python3"`, `args = ["<client home>/core/jezero_client.py", "mcp", "--agent", "codex"]`, and `env = { JEZERO_CLIENT_HOME = "..." }` when the key is not in `~/.jezero` (Codex passes few variables to MCP servers). An old block with `url`, `http_headers` or `bearer_token` is replaced with all its sub-tables (such as `[mcp_servers.jezero.http_headers]`), also when its header is written `[mcp_servers."jezero"]`; a `jezero` block that is not Jezero's bridge is left alone. An entry `setup` cannot edit safely (an inline table such as `jezero = { ... }` under `[mcp_servers]`, dotted keys such as `mcp_servers.jezero.url = ...`, or a second `[mcp_servers.jezero]`) is left alone too, with a message saying what to remove. The rest of the file stays byte for byte, line endings (LF or CRLF) included. The old file is kept as `config.toml.bak-<time>` (mode `0600`), and the new one is written to a temporary file and renamed, keeping the file's mode. With Python 3.11 or newer, `setup` also parses the file before and after the edit and writes nothing unless the result is valid TOML with the expected entry. |
| OpenCode | With `--opencode-plugin PATH`, installs the plugin as `$XDG_CONFIG_HOME/opencode/plugins/jezero.ts` (pointing at a copy of the core in the client home, written as a JSON string literal so a Windows path or a quote in a folder name stays intact) and the `jezero` skill. The plugin registers the MCP server itself. |
| Hermes Agent | Prints the `hermes plugins install` command: the plugin brings its own tools. |
| Antigravity | Antigravity's documentation does not say in which folder plugin hooks and MCP servers run, and the plugin names its core as `./core/jezero_client.py`. So `setup` finds the installed plugin (`jezero` in `~/.gemini/config/plugins`, or in the `plugins` folder of `~/.gemini/antigravity-cli`, `antigravity` or `antigravity-ide`) and rewrites its `hooks.json` and `mcp_config.json` to run the core by its full path. Without an installed plugin it prints the `agy plugin install` command. Run it again after each install or update of the plugin. |

What it prints, for each case:

| Situation | Output |
|---|---|
| Claude Code, no plugin, old HTTP entry | `claude: removed the old user-scope jezero HTTP entry (it held the key in clear); the old entry held the key in clear, and copies of the config may still hold it (Claude Code keeps some in /home/me/.claude/backups): make a new key with ...; MCP server jezero registered (stdio bridge; the key stays in /home/me/.jezero)` |
| Claude Code, bridge already there | `claude: MCP server jezero already registered` |
| Claude Code, plugin installed, old entry | `claude: the Jezero plugin brings the MCP server; nothing to register; removed the user-scope jezero entry (the plugin's own server replaces it)` |
| Claude Code, plugin installed | `claude: the Jezero plugin brings the MCP server; nothing to register` |
| Codex, no entry | `codex: MCP server jezero added to config.toml (stdio bridge; key in /home/me/.jezero) (previous file: /home/me/.codex/config.toml.bak-20261022T101500)` (no backup when there was no file) |
| Codex, old HTTP entry | ``codex: replaced the old jezero entry in config.toml (it held the key in clear) with the stdio bridge. The previous file, /home/me/.codex/config.toml.bak-20261022T101500, still holds the key: delete it once the new file works, then make a new key with `jezero_client.py init --force` and ask the Jezero admin to run the `jezero device rotate` line it prints`` |
| Codex, bridge already there | `codex: MCP server jezero already in config.toml` |
| Codex, an entry it cannot edit | `codex: /home/me/.codex/config.toml defines the jezero MCP server in a form setup cannot edit safely (...). Remove every mcp_servers.jezero entry from it, then run setup again; the file was not changed` |
| Antigravity, plugin installed | `antigravity: plugin in /home/me/.gemini/config/plugins/jezero now runs the core by its full path` |

`setup` never writes the key into an agent's configuration. Only OpenCode's plugin passes
the key to OpenCode in memory, as a header of the remote server it registers.

## HTTP

The core never follows a redirect: every request carries the key, and a redirect may lead
to another host or from HTTPS to plain HTTP. A 3xx answer becomes an error that names
where it pointed, ``the server answered HTTP 302 redirecting URL to LOCATION; the client
does not follow redirects, since the request carries the key: put the final address in
`url` of /home/me/.jezero/client.json``, which uploads log and the bridge returns.

`init` accepts `https://` URLs, and `http://` only for this computer (`localhost`,
`127.0.0.1` and the rest of `127.0.0.0/8`, `::1`). For another computer over plain HTTP,
which sends the key in clear, pass `--insecure-http` (for example on a private VPN);
otherwise `init` exits 2 with `url: http://... is plain HTTP: the key would travel in
clear. Use https://, or pass --insecure-http if the network itself is private (for
example a VPN)`.

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
| Uploaded files remembered in `state.json` | 2000 (the newest) |
| Rejected files remembered in `state.json` | 500 (the newest) |
| Rejected files listed by `status` | 5 (the newest) |
| Upload throttle on `stop` | once per 15 minutes per session |
| Hermes upload throttle (`on_session_end`) | once per 10 minutes per session |
| Upload request | 60 s |
| Biggest file uploaded | `max_upload_mb`, 64 MB |
| Cut-off connections before a large file (over 1 MB compressed) is rejected | 3 |
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
