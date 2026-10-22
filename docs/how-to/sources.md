# Add and tune sources

Bring a new source into Jezero without flooding the library: try it on a few sessions,
check the result, then let it run on its own. This guide also covers projects,
summarising again, redaction and the summarizer settings worth tuning.

## Before you start

- A working server, as in [Run Jezero as a service](deploy.md). The commands below run in
  the folder that holds `jezero.toml`, with `.venv/bin/jezero` or `uv run jezero`.
- Read access, from the server, to the source you want to add (an agent's session folder
  or database), or a computer that uploads it with the plugins (see [Manage computers and
  keys](devices.md)).

## What a connector is

A [connector](../reference/connectors.md) reads one source and turns what is new into
documents for the summarizer. Each one is a `[[connectors]]` block in `jezero.toml`:

- `name` is the source's name. It appears on every item, and
  [clients](../reference/configuration.md#clients) are limited to sources by name
  patterns, such as `agent-*`. Lowercase letters, digits, `-` and `_`.
- `type` picks the implementation.
- `options` holds the settings of that type: paths, filters, how long to wait.

Each connector keeps a cursor, so a run only reads what changed since the last one, and a
session already stored with the same text is never summarised twice.

| Type | Reads |
|---|---|
| `claude_code` | Claude Code transcripts (`*.jsonl` under a projects folder). |
| `codex` | Codex CLI transcripts (`*.jsonl` under a sessions folder; the path may be a glob). |
| `hermes` | Hermes Agent sessions, from its `state.db` or from device uploads. |
| `opencode` | OpenCode sessions, from its SQLite store or from device uploads. |
| `antigravity` | Antigravity conversations, from its local store or from device uploads. |
| `paperclip` | Issues of the Paperclip orchestrator, with comments and documents, through its API. |

`jsonl` is not a connector to schedule: load such files with
[`jezero import-jsonl`](../reference/cli.md#jezero-import-jsonl). A `[[connectors]]` block
of type `jsonl` stops the config from loading (``connector 'notes': jsonl is import-only:
use `jezero import-jsonl` instead of a [[connectors]] entry``). `gmail` is planned.
`jezero check-config` marks the types your version does not implement yet. Every option
of every type is in [Connectors](../reference/connectors.md).

## Add a source

Pilot every new source before it runs on a schedule: it is cheaper to fix a filter on
three sessions than on three thousand.

1. Add the connector, disabled. For example, Claude Code sessions on the server:

   ```toml
   [[connectors]]
   name = "agent-claude-code"
   type = "claude_code"
   enabled = false
   interval_minutes = 15
   [connectors.options]
   projects_dir = "~/.claude/projects"
   ```

   Sessions from other computers need no block written by hand:
   [`jezero device approve`](../reference/cli.md#jezero-device-approve) adds one disabled
   connector per agent for that computer.

2. Check that the config loads:

   ```bash
   jezero check-config
   ```

   You should see the connector listed:

   ```text
   connectors:
     - agent-claude-code [claude_code] disabled
   ```

3. Count the work without calling the model. `--connector` runs a connector even when it
   is disabled; `--dry-run` stores nothing and leaves the cursor alone:

   ```bash
   jezero run --connector agent-claude-code --dry-run
   ```

   You should see:

   ```json
   {"connector": "agent-claude-code", "docs": 3, "unchanged": 0, "stored": 0, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 3186, "input_tokens": 0, "output_tokens": 0, "pruned": 0, "dry_run": true, "sample": false, "note": null, "errors": [], "warnings": []}
   ```

   `docs` is how many documents the first full run would summarise (a long session gives
   several), `input_chars` how much text the model would read, and `deferred_sessions`
   the sessions changed in the last `settle_minutes` (30 by default), which wait until
   they are quiet. If the numbers are far from what you expect, tighten the filters (each
   type has its own, such as `skip_cwd_globs` or `min_chars`) and count again. A path
   option that points nowhere fails here already, with `FileNotFoundError: projects_dir
   folder not found: PATH`.

4. Summarise a sample. `--sample N` takes the newest N sessions and stores their items.
   It is a pilot: the connector's cursor, last run and last success stay as they were, so
   the schedule and `jezero health` still see a connector that never ran:

   ```bash
   jezero run --connector agent-claude-code --sample 2
   ```

   You should see `"stored"` above 0, `"failed": 0` and `"sample": true`:

   ```json
   {"connector": "agent-claude-code", "docs": 2, "unchanged": 0, "stored": 2, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 2124, "input_tokens": 200, "output_tokens": 100, "pruned": 0, "dry_run": false, "sample": true, "note": null, "errors": [], "warnings": []}
   ```

   When no session is ready yet, the report says `"note": "nothing to summarise"` (with
   the number still deferred) and the pilot needs no summarizer at all.

5. Read what it wrote:

   ```bash
   jezero search "WORDS FROM ONE OF THOSE SESSIONS" --source agent-claude-code
   ```

   You should see one line per item, with its id, date, source, project and title:

   ```text
   [1]   0.000  2026-10-01  agent-claude-code  shop  Fix a parser that drops the header row
   ```

   Check that titles are specific, that projects are right (see
   [Projects](#tag-items-with-the-right-project)) and that nothing secret got through.

6. Enable the connector (`enabled = true`). Since the pilot did not count as a run, the
   next tick of the timer (`jezero run --due`, every 5 minutes) runs it at once. This
   first run reads the whole source, oldest first, and the sampled sessions come back as
   `unchanged`, without summarizer calls:

   ```json
   {"connector": "agent-claude-code", "docs": 3, "unchanged": 2, "stored": 1, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 1062, "input_tokens": 100, "output_tokens": 50, "pruned": 0, "dry_run": false, "sample": false, "note": null, "errors": [], "warnings": []}
   {"due": {"ran": ["agent-claude-code"], "not_due_until": {}}}
   ```

   For a large source, run it by hand in steps instead, with `--limit N`, which processes
   the oldest N sessions and moves the cursor; repeat until `docs` reaches 0. If the timer
   fires during your run, it skips with `{"skipped": "another run is in progress"}`.

From then on the timer runs it. [Keep an eye on Jezero](monitor.md) tells you if it stops.

## Follow the agents of an orchestrator

Orchestrators that run many agents often give each one its own config folder, so their
sessions are not in your `~/.claude/projects` or `~/.codex/sessions`. One connector can
follow all of them with a glob, and picks up agents added later:

```toml
[[connectors]]
name = "agent-claude-code-team"
type = "claude_code"
enabled = false
[connectors.options]
projects_dir = "~/agents/claude-*/.claude/projects"

[[connectors]]
name = "agent-codex-team"
type = "codex"
enabled = false
[connectors.options]
sessions_dir = "~/agents/*/codex-home/sessions"
```

Each Claude Code session tells the summarizer which folder it came from
(`config_dir=claude-atlas`), so summaries can name the agent. Pilot it like any new
source, then decide which keys may read it: add the source name to the `sources` of the
keys the agents themselves use if they should see each other's work.

## Choose how often it runs

The timer starts `jezero run --due` every 5 minutes. `--due` runs each enabled connector
whose `interval_minutes` (15 by default) has passed since its last run, successful or not.
A value below 5 therefore means every 5 minutes, and a broken connector retries once per
interval, not on every tick.

How fresh items are also depends on `settle_minutes`: a session is read only once it has
been quiet that long, so a session reaches Jezero roughly between `settle_minutes` and
`settle_minutes + interval_minutes` after its last message.

## Tag items with the right project

Each item gets a project, so agents can search one project and see its recent work.
Jezero takes it from the source when it can, which beats the model's guess:

1. A device's answer: sessions uploaded by the plugins carry the project their computer
   found.
2. `[projects] paths`: folders you map by hand. `~` matches any home folder, on any
   computer. The longest match wins.
3. The git repository the session ran in; for a worktree, the main repository.
4. `[projects] roots`: the folder right below one of these names (`projects`, `code`,
   `dev`, `src`, `repos` and other common ones by default).
5. The connector's own `project`, a default for everything it reads.
6. Nothing found: the summarizer chooses, preferring the slugs in
   `summarizer.known_projects`.

`aliases` rename the result, for every connector. The session connectors also accept a
`project_aliases` option that applies to that connector only. `paperclip` maps its own
project names with `project_map`, and the `[projects]` aliases and its `project_aliases`
then rename the result, as for the other connectors.

1. Add your rules:

   ```toml
   [projects]
   paths = { "~/work/billing-api" = "billing", "/srv/stacks" = "atlas" }
   aliases = { "shop-web" = "shop", "atlas-wt" = "atlas" }

   [summarizer]
   known_projects = ["atlas", "billing", "shop"]
   ```

2. Preview what changes for items already stored. `jezero retag` reads the source again
   from the start and applies the current rules, without calling the model:

   ```bash
   jezero retag --connector agent-claude-code --dry-run
   ```

   You should see how many items would change:

   ```json
   {"connector": "agent-claude-code", "docs": 3, "retagged": 1, "dry_run": true}
   ```

3. Apply it:

   ```bash
   jezero retag --connector agent-claude-code
   ```

`retag` only changes items whose source gives a project; items where the model chose stay
as they are. The git rule needs the session's folder to exist on the server. For
sessions from other computers, set the same rules in the device's
`~/.jezero/client.json`, under `"projects"`, so its plugin tags them before upload (see
[Client](../reference/client.md)).

## Summarise items again

Summarise stored items again after you change the model or `output_language`, or when a
batch came out poor. Jezero works from the original text it kept (after redaction), and
keeps each item's source, id, date and project. It calls the model once per item.

1. Find the ids. `jezero search` prints them in brackets. For every item of one source:

   ```bash
   python3 -c "import sqlite3; db = sqlite3.connect('file:data/jezero.db?mode=ro', uri=True); [print(r[0]) for r in db.execute(\"SELECT id FROM items WHERE source = 'agent-claude-code'\")]" > ids.txt
   ```

2. Summarise them:

   ```bash
   jezero resummarize --ids-file ids.txt
   ```

   Or name a few with `--ids 12,15,31`. You should see a report like the one of a run,
   with `"stored"` equal to the number of items and `"failed": 0`.

## Hide more secrets

Jezero redacts common credential shapes (API keys, tokens, private keys, passwords in
assignments and URLs) before anything is summarised, stored or indexed. Add your own
patterns for what only you know, such as internal host names or customer ids.

1. Add regular expressions (Python syntax) to `[redaction]`. Single quotes keep
   backslashes as they are:

   ```toml
   [redaction]
   enabled = true
   extra_patterns = ['\b[a-z0-9-]+\.corp\.example\b', '\bCUST-[0-9]{6}\b']
   ```

   Each match becomes `[REDACTED:custom]`. An invalid pattern makes the config fail to
   load, with the error.

2. New sessions are redacted with the new rules from the next run. For items already
   stored, preview, then apply:

   ```bash
   jezero redact-stored --dry-run
   jezero redact-stored
   ```

   You should see:

   ```text
   would redact 120 secrets in 3 items
   redacted 120 secrets in 3 items
   ```

   It rewrites titles, summaries, questions, atoms and the kept original text, and keeps
   the search index in sync. It makes no model calls.

> [!WARNING]
> Redaction catches common shapes; it is not a guarantee. Keep sources known to carry
> secrets out with the connector's filters, such as `skip_cwd_globs` or
> `skip_dir_globs`. Files uploaded to the inbox are kept as sent: redaction applies when
> they are read, not to the files.

## Tune the summarizer

The [summarizer](../explanation/summaries.md) settings live in
[`[summarizer]`](../reference/configuration.md#summarizer). The ones worth tuning:

| Key | Default | Change it when |
|---|---|---|
| `model` | none | Always set it. A small, fast model is enough; try a few items with `--sample` before a backfill. |
| `output_language` | the document's | Your sessions mix languages. One language for every item lets one query find them all. Run `resummarize` on older items afterwards. |
| `max_input_chars` | `60000` | Long sessions are split into parts of at most this size, each its own item. Raise it for fewer, broader items, within the model's context window. |
| `max_concurrency` | `4` | You hit rate limits (lower it) or a backfill is slow and your quota allows more (raise it). Jezero already retries on `429` and server errors. |
| `timeout_seconds` | `120` | A slow local model times out on long parts. |
| `known_projects` | empty | The model invents project names for sessions without a folder hint. |

> [!NOTE]
> Set `max_input_chars` before the first backfill. Parts are identified by where they
> start, so after a change a session that is read again is cut differently and its parts
> are summarised again. Parts that no longer exist are removed (`pruned` in the report).
> Sessions read before the change keep their old parts until they are read again: see
> [Read a source again](#read-a-source-again).

## Read a source again

A connector reads only what changed since its cursor. To make it read its whole source
again, for example after you changed its filters, `[redaction]` or `max_input_chars`, or
after an update that reads more of a format, forget the cursor:

```bash
jezero connector reset agent-claude-code --yes
```

You should see:

```text
reset agent-claude-code: its next run reads the whole source again. Stored items are kept; units whose redacted text is unchanged are skipped by content hash, without summarizer calls.
```

Without `--yes` it asks first, and in a script without a terminal it refuses with
`error: pass --yes to confirm (no terminal to ask)`. A connector that never stored a
cursor answers `agent-claude-code has no cursor yet: its next run reads everything
anyway`. It also refuses while a run is in progress.

The next run, by hand or by the timer, then reads everything. What it costs depends on
what you changed:

- Nothing that changes the text sent to the model: every unit comes back `unchanged`, and
  the run makes no summarizer calls.

  ```json
  {"connector": "agent-claude-code", "docs": 3, "unchanged": 3, "stored": 0, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 0, "input_tokens": 0, "output_tokens": 0, "pruned": 0, "dry_run": false, "sample": false, "note": "nothing to summarise", "errors": [], "warnings": []}
  ```

- Redaction rules, filters that change the rendered text, or `max_input_chars`: the
  units whose text changed are summarised again, one call each. With a new
  `max_input_chars`, parts that no longer exist are removed:

  ```json
  {"connector": "agent-claude-code", "docs": 3, "unchanged": 0, "stored": 3, "failed": 0, "deferred_sessions": 0, "redactions": 0, "input_chars": 3186, "input_tokens": 300, "output_tokens": 150, "pruned": 3, "dry_run": false, "sample": false, "note": null, "errors": [], "warnings": []}
  ```

Run `jezero run --connector NAME --dry-run` after the reset to see `docs` and
`input_chars` before you pay for it. To only fix projects, use `jezero retag`, which
calls no model; to change the summaries themselves, `jezero resummarize`.

## Next steps

- [Connectors](../reference/connectors.md): every option of every type.
- [Measure search quality](measure-search.md): check that a new source does not push answers down.
- [Keep an eye on Jezero](monitor.md): alerts when a source stops.
- [Manage computers and keys](devices.md): sessions from other computers, and which
  sources each key may read.
- [How summaries are made](../explanation/summaries.md): items, atoms and what they cost.
