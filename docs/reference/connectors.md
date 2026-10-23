# Connectors

Every connector type, what it reads, its options with their defaults, and what it keeps.
A connector is one `[[connectors]]` block of `jezero.toml`; its `name` becomes the
`source` of the items it produces.

## Types

| Type | Status | Reads | Section |
|---|---|---|---|
| `claude_code` | available | Claude Code transcripts (`*.jsonl` under a projects folder) | [claude_code](#claude_code) |
| `codex` | available | Codex CLI rollouts (`*.jsonl`, `*.jsonl.zst` under a sessions folder) | [codex](#codex) |
| `opencode` | available | OpenCode's SQLite store, or session exports uploaded by a device | [opencode](#opencode) |
| `hermes` | available | Hermes Agent's `state.db`, or session exports uploaded by a device | [hermes](#hermes) |
| `antigravity` | available | Antigravity's local store, or transcripts uploaded by a device | [antigravity](#antigravity) |
| `paperclip` | available | Paperclip tasks: issues with comments, documents and decisions, over its API | [paperclip](#paperclip) |
| `jsonl` | available | Pre-summarised items, loaded by `jezero import-jsonl`; a `[[connectors]]` block of this type is refused | [jsonl](#jsonl) |
| `gmail` | planned | Not implemented: listed so `check-config` can tell it from a typo | [gmail](#gmail) |

`jezero check-config` lists the configured connectors and marks planned types as "not
implemented yet". A `type` that is not in this table is a configuration error.

Other packages can add types through the `jezero.connectors` entry point group, pointing at
a `ConnectorType`:

```toml
[project.entry-points."jezero.connectors"]
mytool = "jezero_mytool:CONNECTOR_TYPE"
```

A built-in type wins over an add-on with the same name; an add-on that fails to load is
skipped with a warning.

## Common keys

These keys exist for every type. They are validated with the rest of the configuration (see
[`[[connectors]]`](configuration.md#connectors)); everything type-specific goes in
`[connectors.options]`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Unique, `^[a-z0-9][a-z0-9_-]{0,63}$`. Becomes the `source` of every item. |
| `type` | string | required | One of the [types](#types). |
| `enabled` | boolean | `true` | `jezero run` without `--connector` skips disabled connectors; `jezero health` ignores them. |
| `interval_minutes` | integer (≥ 1) | `15` | `jezero run --due` runs the connector only when this long has passed since its last run. |
| `project` | string | none | Project for items whose source gives none (see [Projects](#projects)). |
| `trust` | `"own"` \| `"external"` | the type's own | Copied to every item. `external` marks text written by third parties. Unset, it takes the type's default: `external` for `gmail`, `own` for every other type. |
| `options` | table | empty | Type-specific options, below. |

Option keys that look like a secret (`api_key`, `token`, `secret`, `password`, `passwd`,
`credentials`, `private_key`, alone or as a suffix) are rejected unless they end in `_env`
and name an environment variable.

## Source names

The `name` is free, within the pattern above. Two conventions are used:

| Where the sessions are | Name | Example |
|---|---|---|
| On the server's own computer | `agent-<agent>` | `agent-claude-code`, `agent-codex` |
| Uploaded by a device | `<prefix>-<agent>-<device>` | `agent-codex-laptop`, `work-codex-work-mac` |

`jezero device approve DEVICE SHA256` writes one disabled connector per agent for the
device, with the default prefix `agent` (change it with `--source-prefix`, see
[`jezero device approve`](cli.md#jezero-device-approve)). `<agent>` is the inbox name of the
agent, and each connector reads the device's inbox folder:

| `<agent>` | Connector type | Option written | Folder |
|---|---|---|---|
| `claude-code` | `claude_code` | `projects_dir` | `<inbox>/<device>/claude-code/projects` |
| `codex` | `codex` | `sessions_dir` | `<inbox>/<device>/codex/sessions` |
| `opencode` | `opencode` | `export_dir` | `<inbox>/<device>/opencode/sessions` |
| `hermes` | `hermes` | `export_dir` | `<inbox>/<device>/hermes/sessions` |
| `antigravity` | `antigravity` | `transcripts_dir` | `<inbox>/<device>/antigravity/brain` |

Client keys are limited to sources by glob (`sources = ["agent-*"]`), so the prefix decides
which keys can read a device's sessions.

## How a run works

The same steps apply to every scheduled type.

1. **Collect.** The connector lists the units (sessions, conversations, issues) changed
   since its cursor. The cursor is a timestamp: a file's modification time, a session's
   last message time, or an issue's last activity. A folder or file named in the options
   that does not exist fails the run with `OPTION folder not found: PATH` (or `file not
   found`), so a typo shows up in `jezero health` instead of as an empty source.
   A unit that cannot be read at all (a file that is not valid for its format, such as an
   export that is JSON but not an object) is skipped and named in the run's `warnings` and
   in `jezero health` until the next run; the other units go on, and the cursor moves past
   it, since reading the same bytes again cannot help. Errors that may pass, such as a
   locked database or an HTTP error from Paperclip, fail the run instead and keep the
   cursor.
2. **Settle.** Units changed in the last `settle_minutes` are deferred to a later run, so
   an ongoing session is not summarised every few minutes. `jezero run` reports them as
   `deferred_sessions`.
3. **Render.** Each unit becomes plain text, one line per message or tool call, each line
   starting with its UTC time: `[2026-10-21 09:12] user: ...`. Plumbing is dropped (see
   each type).
4. **Segment.** Text longer than `summarizer.max_input_chars` (default 60000) is split into
   [segments](data-model.md#segments-of-long-sessions), one item each.
5. **Redact, compare, summarise.** Secrets are redacted, in the text and in what goes to
   the summarizer with it (the title hint and the context values). A segment whose
   redacted text has the same SHA-256 as the stored item is skipped as `unchanged` (if the
   source now gives another `original_ref`, the stored one is updated, with no summarizer
   call); the others go to the summarizer and are inserted or updated. Terminal control
   characters are dropped from what the summarizer writes. A segment the summarizer fails
   on (an error, or an answer of the wrong shape) is counted in `failed`; the other
   segments of the run are still stored.
6. **Prune.** When a unit is read again whole and no longer produces some of its stored
   segments (`max_input_chars` changed, or the session got shorter), those items are
   removed and counted in `pruned`. Nothing is pruned for a unit with a failed segment,
   and a unit the source stops producing altogether is kept.
7. **Advance.** The cursor moves only when every segment was stored and the connector
   reported no warning. Otherwise the same units are read again next run, and the error is
   kept in the connector's state for `jezero health`, redacted like the text (and with the
   summarizer's API key replaced). A source that cannot be read for now (a locked or
   damaged database, a missing network folder, no memory left) fails the run and keeps the
   cursor; a unit that is itself malformed is skipped and reported.

`--limit N` processes at most N units, oldest first, and more when the Nth unit shares its
timestamp with the next ones: the whole group is taken, because the next run only reads
units newer than the cursor. `--sample N` processes the newest N
units as a pilot: it stores their items but leaves the cursor, the last run and the last
success where they were, and needs no summarizer when nothing is ready (see [`jezero
run`](cli.md#jezero-run)). [`jezero connector reset`](cli.md#jezero-connector-reset)
forgets the cursor, so the next run reads the whole source again; unchanged units are
skipped by their hash.

### Projects

Agent connectors find each item's project from the folder the session ran in, with the
rules of [`[projects]`](configuration.md#projects), first match wins:

1. The `<session file>.project` file next to an uploaded session: the project the device's
   client found (see [the client](client.md#projects)).
2. `[projects].paths`: an explicit folder, longest match.
3. The git repository holding the folder (the main repository for a worktree), when the
   folder exists on the server and `[projects].git` is not `false`.
4. `[projects].roots`: the folder right below one of these names, case insensitive. The
   default list is `projects`, `code`, `dev`, `src`, `repos`, `repositories`, `git`,
   `github`, `gitlab`, `workspace`, `workspaces`, `work`, `developer` and `sites`; setting
   `roots` replaces it. Only folders below the home folder count, so for a user named
   `dev`, a `notes` folder in their home has no project.

A folder name works as a project when it is letters (of any alphabet, so `diseño` counts),
digits, `.`, `_` and `-`, starting with a letter or digit.

The result is lowercased (and normalised to one Unicode form, as macOS writes accents
differently), then renamed by `[projects].aliases` merged with the connector's
`project_aliases` option (the option wins). If no rule matches, the connector's `project`
key is used, and if that is not set either, the summarizer's guess. `paperclip` uses its
own `project_map` instead of the folder rules, then renames the result with the same
aliases.

### Prompt cleaning

`codex`, `opencode` and `antigravity` accept two options for orchestrators that repeat the
same long instructions at the top of every prompt. Both apply to user messages only.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `user_prompt_markers` | list of regex | `[]` | Keep a message only from the first match of the first marker that matches (multiline mode). |
| `user_prompt_drop_lines` | list of regex | `[]` | Drop every line that matches one of them, after the markers. |

### Read-only commands

`codex`, `opencode` and `antigravity` drop shell commands that only inspect, unless
`keep_read_only_commands = true`. A command is read-only when it starts with `sed -n`,
`rg`, `grep`, `cat`, `head`, `tail`, `ls`, `nl`, `wc`, `find`, `pwd`, `tree`, `stat`,
`file`, `which`, or `git status|diff|log|show|branch|rev-parse|ls-files`.

## claude_code

Claude Code session transcripts. Claude Code writes one JSON Lines file per session under
a projects folder, one subfolder per working directory, and deletes old ones by itself (30
days by default): the library keeps the text once it is ingested.

- **Unit:** one `*.jsonl` file, found recursively under `projects_dir`. Cursor: the file's
  modification time. With a glob, the same file path (project folder and session) found
  in two homes is read once, from the newest copy: a home copied to make another never
  overwrites newer work.
- **Kept:** user and assistant text; one line per tool call (`tool call: NAME ARGS`); the
  result of the tools in `keep_results_of` (`Task result: ...`); slash commands as
  `(command) NAME ARGS`; command output as `(output) ...`, shortened.
- **Dropped:** `<system-reminder>` and `<memory-context>` blocks, meta records,
  "Caveat: The messages below were generated by the user..." notices, other tool results,
  side-chain records and sub-agent transcripts (`<session>/subagents/agent-*.jsonl`)
  unless `include_subagents`.
- **Title:** the session's `ai-title` record, else its `summary` record.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `projects_dir` | path or glob | required | Claude Code's projects folder, usually `~/.claude/projects`. A glob (`~/agents/claude-*/.claude/projects`) follows the config folder of every agent an orchestrator runs, including ones added later, and tells the summarizer which one each session came from (`config_dir=claude-atlas`). A missing folder fails the run. A glob that matches nothing yet does not, but the folder before its first wildcard must exist. |
| `settle_minutes` | number | `30` | Wait until a file has not changed for this long. |
| `include_subagents` | boolean | `false` | Also read `subagents/` transcripts and side-chain records. |
| `include_tool_calls` | boolean | `true` | One line per tool call. |
| `max_tool_args_chars` | integer | `200` | Tool call arguments (as JSON) are cut here, with `...`. |
| `keep_results_of` | list of string | `["Task", "Agent"]` | Tools whose result is kept: the report of a delegated task. |
| `max_kept_result_chars` | integer | `3000` | Kept results are shortened in the middle to this length. |
| `max_command_output_chars` | integer | `500` | Local command output is shortened in the middle to this length. |
| `skip_entrypoints` | list of string | `[]` | Skip sessions whose `entrypoint` is listed (for example `sdk-cli`). |
| `skip_project_dirs` | list of glob | `[]` | Skip sessions whose project folder (Claude Code's slug of the working directory) matches. |
| `min_chars` | integer | `400` | Skip sessions whose rendered text is shorter. |
| `project_aliases` | table of string | `{}` | Rename detected projects, on top of `[projects].aliases`. |

| Item field | Value |
|---|---|
| `external_id` | `<sessionId>#<line>`: the 0-based line of the segment's first record. A sub-agent transcript (with `include_subagents`) is its own unit, `<sessionId>/<file name>#<line>` (`4b1c.../agent-a1b2#0`), since its records carry the parent's sessionId. |
| `original_ref` | `claude-code-session:<sessionId>`, or `claude-code-session:<sessionId>/<file name>` for a sub-agent transcript |
| Project folder | the session's `cwd` |

```toml
[[connectors]]
name = "agent-claude-code"
type = "claude_code"
interval_minutes = 15
[connectors.options]
projects_dir = "~/.claude/projects"
settle_minutes = 30
keep_results_of = ["Task", "Agent"]
min_chars = 400
skip_project_dirs = ["-tmp-*"]
```

## codex

OpenAI Codex CLI sessions. Codex writes one JSON Lines "rollout" per session under
`sessions/YYYY/MM/DD/`; newer builds compress them (`.jsonl.zst`, read across all the
zstd frames of the file).

- **Unit:** one `*.jsonl` or `*.jsonl.zst` file, found recursively under `sessions_dir`.
  Cursor: the file's modification time.
- **Kept:** user and assistant messages; one short line per tool call: `shell: CMD ; CMD`
  for commands, `patch: add PATH, update PATH` for patches, `tool call: NAME ARGS` for
  the rest.
- **Dropped:** developer and system messages; text Codex injects into user messages
  (`<environment_context>`, `<recommended_plugins>`, `<user_instructions>`,
  `<skills_instructions>`, `<permissions instructions>`, `<turn_aborted>`,
  `# AGENTS.md instructions`, and blocks not marked as typed by the user); reasoning; tool
  output; token accounting; polling of running commands (`write_stdin`);
  [read-only commands](#read-only-commands); sessions started by Codex as sub-agents
  (unless `include_subagents`).
- **Title:** the session's `thread_name` from `session_index.jsonl`.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `sessions_dir` | path or glob | required | Codex's sessions folder, usually `~/.codex/sessions`. A glob (`~/agents/*/codex-home/sessions`) follows several homes, including ones added later. A missing folder fails the run. A glob that matches nothing yet does not, but the folder before its first wildcard must exist (`sessions_dir folder not found: /home/me/agents`). |
| `session_index` | path | `<sessions_dir>/../session_index.jsonl` | Where session titles are read from. |
| `settle_minutes` | number | `30` | Wait until a file has not changed for this long. |
| `include_subagents` | boolean | `false` | Also read sessions whose `session_meta.source` marks a sub-agent. |
| `include_tool_calls` | boolean | `true` | One line per tool call. |
| `max_tool_args_chars` | integer | `200` | Tool call lines are cut here, with `...`. |
| `keep_read_only_commands` | boolean | `false` | Keep [read-only commands](#read-only-commands). |
| `skip_tools` | list of string | `["wait", "sleep", "list_agents"]` | Tool names never shown. |
| `skip_cwd_globs` | list of glob | `[]` | Skip sessions whose working directory matches. |
| `min_chars` | integer | `400` | Skip sessions whose rendered text is shorter. |
| `project_aliases` | table of string | `{}` | Rename detected projects, on top of `[projects].aliases`. |
| `user_prompt_markers` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |
| `user_prompt_drop_lines` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |

| Item field | Value |
|---|---|
| `external_id` | `<session id>#<line>`: the 0-based line of the segment's first record |
| `original_ref` | `codex-session:<session id>` |
| Project folder | `cwd` of the `session_meta` record |

```toml
[[connectors]]
name = "agent-codex"
type = "codex"
interval_minutes = 15
[connectors.options]
sessions_dir = "~/.codex/sessions"
skip_cwd_globs = ["*/scratch-*"]
project_aliases = { "shop-wt" = "shop" }
```

## opencode

OpenCode sessions, from its SQLite store (opened read-only; the `event` table is never
read) or from the session exports the OpenCode plugin uploads from another computer.

- **Unit:** one session. Cursor: the session's `time_updated` (store) or the export file's
  modification time (`export_dir`).
- **Kept:** user and assistant text; one line per tool call that changes something:
  `shell: CMD`, `edit: PATH` (also `write`, `patch`, `multiedit`), `tool call: NAME ARGS`
  for the rest, with `(failed)` when the call ended in an error.
- **Dropped:** reasoning, step markers, patches, snapshots, tool output, the tools `read`,
  `grep`, `glob`, `list`, `todoread`, `webfetch` and `invalid` and
  [read-only commands](#read-only-commands) (unless `keep_read_only_commands`), sub-agent
  sessions (unless `include_subagents`).
- **Title:** the session's title.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `db_path` | path | `~/.local/share/opencode/opencode.db` | OpenCode's database. Missing file: the run fails. |
| `export_dir` | path | none | Read `*.json` exports in this folder instead of the database. Missing folder: the run fails. |
| `settle_minutes` | number | `30` | Wait until a session has not changed for this long. |
| `include_subagents` | boolean | `false` | Also read sessions that have a parent session. |
| `include_tool_calls` | boolean | `true` | One line per tool call. |
| `max_tool_args_chars` | integer | `200` | Tool call lines are cut here, with `...`. |
| `keep_read_only_commands` | boolean | `false` | Keep read-only tools and [read-only commands](#read-only-commands). |
| `skip_tools` | list of string | `["todowrite"]` | Tool names never shown. |
| `skip_dir_globs` | list of glob | `[]` | Skip sessions whose directory matches. |
| `min_chars` | integer | `400` | Skip sessions whose rendered text is shorter. |
| `project_aliases` | table of string | `{}` | Rename detected projects, on top of `[projects].aliases`. |
| `user_prompt_markers` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |
| `user_prompt_drop_lines` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |

An export is `{"session": {...}, "messages": [{"info": {...}, "parts": [...]}]}`, as the
OpenCode SDK returns them.

| Item field | Value |
|---|---|
| `external_id` | `<session id>#<n>`: the 0-based position of the segment's first rendered block |
| `original_ref` | `opencode-session:<session id>` |
| Project folder | the session's `directory` |

```toml
[[connectors]]
name = "agent-opencode"
type = "opencode"
interval_minutes = 15
[connectors.options]
db_path = "~/.local/share/opencode/opencode.db"
skip_dir_globs = ["/tmp/*"]
```

## hermes

Hermes Agent sessions, from a profile's `state.db` (opened read-only) or from the session
exports the Hermes plugin uploads from another computer. Set exactly one of `state_db` and
`export_dir`; both or neither is an error.

- **Unit:** one session. Cursor: the time of its last message (`state_db`) or the export
  file's modification time (`export_dir`).
- **Kept:** user and assistant messages (only active ones, when the database marks them);
  one line per assistant tool call (`tool call: NAME ARGS`).
- **Dropped:** tool results, `<memory-context>` and `<system-reminder>` blocks,
  `[System note: ...]` notes, sessions whose `source` is in `skip_session_sources`.
- **Title:** the session's title.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `state_db` | path | none | A Hermes profile's `state.db`. Missing file: the run fails. |
| `export_dir` | path | none | Read `<session>.json` exports in this folder. Missing folder: the run fails. |
| `skip_session_sources` | list of string | `["cron", "subagent"]` | Session sources (channels) to skip. |
| `settle_minutes` | number | `30` | Wait until a session has been quiet for this long. |
| `include_tool_calls` | boolean | `true` | One line per tool call. |
| `max_tool_args_chars` | integer | `200` | Tool call arguments are cut here, with `...`. |
| `min_chars` | integer | `400` | Skip sessions whose kept dialogue is shorter, such as a lone greeting. |
| `project_aliases` | table of string | `{}` | Rename detected projects, on top of `[projects].aliases`. |

With `--sample N`, sessions skipped by `min_chars` do not count towards N.
The export format is described in [the client reference](client.md#hermes-exports).

| Item field | Value |
|---|---|
| `external_id` | `<session id>#<message id>`: the database id of the segment's first message |
| `original_ref` | `hermes-session:<session id>` |
| Project folder | the session's `cwd`, when the database has that column |

```toml
[[connectors]]
name = "agent-hermes"
type = "hermes"
interval_minutes = 15
[connectors.options]
state_db = "~/.hermes/state.db"
skip_session_sources = ["cron", "subagent"]
```

## antigravity

Antigravity conversations, from its local store or from the transcripts the Antigravity
plugin uploads from another computer.

The local store keeps one SQLite file per conversation, `<data_dir>/conversations/<id>.db`,
with protobuf steps and no published schema. The connector reads only the fields observed to
be stable (user input, agent replies, tool calls with their short `toolSummary`) and skips
whatever does not parse. Titles and workspaces come from
`<data_dir>/conversation_summaries.db` when it exists.

An uploaded transcript is `<conversation id>/transcript.jsonl`, Antigravity's JSON log:
`USER_INPUT` steps (the text inside `<USER_REQUEST>`) and `PLANNER_RESPONSE` steps.

- **Unit:** one conversation. Cursor: the file's modification time.
- **Kept:** user requests, agent replies, one line per tool call: `shell (SUMMARY): CMD`,
  `edit: PATH`, `web search: QUERY`, `read url: URL`, else `NAME: SUMMARY` when the call
  has a summary.
- **Dropped:** tool results, thinking notes, task notifications, context checkpoints, the
  file-reading tools (`view_file`, `read_file`, `list_dir`, `grep_search`,
  `find_by_name`) and [read-only commands](#read-only-commands) (unless
  `keep_read_only_commands`), any other tool call without a summary.
- **Title:** from `conversation_summaries.db` (local store only).

| Option | Type | Default | Meaning |
|---|---|---|---|
| `data_dir` | path | `~/.gemini/antigravity-cli` | Antigravity's data folder. |
| `transcripts_dir` | path | none | Read `*/transcript.jsonl` here instead of `data_dir`. |
| `settle_minutes` | number | `30` | Wait until a conversation has not changed for this long. |
| `include_tool_calls` | boolean | `true` | One line per tool call. |
| `max_tool_args_chars` | integer | `200` | Tool call lines are cut here, with `...`. |
| `keep_read_only_commands` | boolean | `false` | Keep file-reading tools and [read-only commands](#read-only-commands). |
| `skip_dir_globs` | list of glob | `[]` | Skip conversations whose workspace matches (local store only). |
| `min_chars` | integer | `400` | Skip conversations whose rendered text is shorter. |
| `unreadable_min_steps` | integer | `2` | A conversation with at least this many steps and nothing readable raises a warning. |
| `project_aliases` | table of string | `{}` | Rename detected projects, on top of `[projects].aliases`. |
| `user_prompt_markers` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |
| `user_prompt_drop_lines` | list of regex | `[]` | See [Prompt cleaning](#prompt-cleaning). |

A missing `data_dir` (`data_dir folder not found: PATH`), a `data_dir` without a
`conversations` folder (`data_dir has no conversations folder: PATH`) or a missing
`transcripts_dir` fails the run. When nothing can be
read from a conversation that has steps, the run reports `format check: nothing readable in
N conversation(s) with steps (...); the Antigravity format may have changed`: the
connector shows as failing in `jezero health` and its cursor stays, until the reader is
updated.

| Item field | Value |
|---|---|
| `external_id` | `<conversation id>#<n>`: the 0-based position of the segment's first rendered block |
| `original_ref` | `antigravity-conversation:<conversation id>` |
| Project folder | the first `file://` workspace (local store); the `.project` file only (uploads) |

```toml
[[connectors]]
name = "agent-antigravity"
type = "antigravity"
interval_minutes = 15
[connectors.options]
data_dir = "~/.gemini/antigravity-cli"
skip_dir_globs = ["/tmp/*"]
```

## paperclip

Paperclip tasks. Paperclip is an orchestrator for teams of agents; its issues hold what
was asked, the plan, each agent's report and the people's answers. The connector only
sends `GET` requests, with the key in `Authorization: Bearer ***`.

- **Unit:** one issue. Cursor: the later of its `lastActivityAt` and `updatedAt`.
- **Kept, in this order:** a header (identifier, title, status, project, assignee, author)
  and the description; then comments, interactions (questions and confirmations with their
  answer) and documents (plans, runbooks, at their last revision), ordered by creation
  time, each with its author.
- **Dropped:** hidden issues, issues in `skip_statuses`, deleted and empty comments, system
  notices (unless `skip_system_notices = false`).
- **Title:** `<identifier> <title>`.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `api_url` | URL | `"http://127.0.0.1:3100/api"` | Paperclip's API. |
| `api_key_env` | string | `"PAPERCLIP_API_KEY"` | Environment variable holding the API key. Unset: the run fails. An agent key scoped to one company is enough. |
| `company_ids` | list of string | `[]` | Companies to read. Empty: the company of the key's agent (`GET /agents/me`). |
| `settle_minutes` | number | `30` | Wait until an issue has been quiet for this long. |
| `include_documents` | boolean | `true` | Add the issue's documents. |
| `include_interactions` | boolean | `true` | Add the issue's interactions. |
| `skip_system_notices` | boolean | `true` | Drop comments presented as system notices. |
| `skip_statuses` | list of string | `[]` | Issue statuses to skip. |
| `project_map` | table of string | `{}` | Paperclip project name → Jezero project. |
| `project_aliases` | table of string | `{}` | Rename the mapped projects, on top of `[projects].aliases` (matched case-insensitively). |
| `board_name` | string | `"board"` | Name shown for comments written by people. |
| `max_block_chars` | integer | `8000` | A description, comment, interaction or document is shortened in the middle to this length. |
| `min_chars` | integer | `200` | Skip issues whose rendered text is shorter. |

Requests, per run: `/agents/me` (only without `company_ids`),
`/companies/<id>/issues?limit=10000`, `/companies/<id>/agents`, `/companies/<id>/projects`
(failures ignored), and per issue `/issues/<id>`, `/issues/<id>/comments`,
`/issues/<id>/interactions` and `/issues/<id>/documents`. Each request times out after 30
seconds.

| Item field | Value |
|---|---|
| `external_id` | `<identifier>#<n>`: `0` for the segment that starts with the header, else the position of its first entry |
| `original_ref` | `paperclip-issue:<company id>/<identifier>`. Items stored with the older `paperclip-issue:<identifier>` get the new form the next time their issue is read, with no summarizer call: `jezero connector reset` and a run update them all. |
| `occurred_at` | the latest time among the segment's entries |
| `project` | `project_map[<project name>]`, renamed by the aliases, else the connector's `project`, else the summarizer's guess |

```toml
[[connectors]]
name = "paperclip"
type = "paperclip"
interval_minutes = 15
[connectors.options]
api_url = "http://127.0.0.1:3100/api"
api_key_env = "JEZERO_PAPERCLIP_API_KEY"
project_map = { "Shop app" = "shop" }
skip_statuses = ["cancelled"]
```

## jsonl

Items that are already summarised, in a JSON Lines file, one item per line. They are
loaded with [`jezero import-jsonl`](cli.md#jezero-import-jsonl), which takes the source name
with `--source`; no `[[connectors]]` block is needed. The record shape is in
[Data model](data-model.md#json-lines-import).

`jsonl` cannot run on a schedule, so a `[[connectors]]` block of this type, enabled or
not, stops the configuration from loading:

```text
Value error, connector 'notes': jsonl is import-only: use `jezero import-jsonl` instead of a [[connectors]] entry
```

## gmail

Planned: Gmail threads through the API with the `gmail.readonly` scope. Mail is written by
third parties, so its items get `trust = "external"` unless the block says otherwise.
Nothing reads it yet: `jezero run`
reports it as `"skipped": "not implemented yet"` and `jezero health` ignores it. Its
options are not defined until it is implemented.

## See also

- [Configuration](configuration.md#connectors): the keys of `[[connectors]]` and
  `[projects]`.
- [Add and tune sources](../how-to/sources.md): connectors in practice, pilots with
  `--sample`.
- [Data model](data-model.md): what each item holds once stored.
- [HTTP API](http-api.md#upload-a-session-file): how devices fill the inbox these
  connectors read.
