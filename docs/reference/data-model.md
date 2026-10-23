# Data model

What the library stores: items and their fields, atoms, sources, projects, trust, the
original text, ids, dates, and how long sessions become several items. Everything lives
in one SQLite file (`[storage].path`).

## Items

An item is one entry of the library: a session, or a segment of a long one, a task or a
thread, summarised. The app calls items *memories*.

| Field | Type | Returned by MCP | Meaning |
|---|---|---|---|
| `id` | integer | yes | Assigned when the item is first stored. It does not change when the item is summarised again. |
| `source` | string | yes | Name of the connector that produced it (see [Sources](#sources)). |
| `external_id` | string | yes | Id inside the source, unique per source. `#N` marks a [segment](#segments-of-long-sessions). |
| `occurred_at` | datetime | yes | When it happened, in UTC (see [Dates](#dates)). |
| `ingested_at` | datetime | no | When it was last inserted or updated, in UTC. |
| `title` | string | yes | Specific, written by the summarizer, at most 200 characters. |
| `summary` | string | yes | Two to five sentences: the goal, what was done, how it ended. |
| `questions` | list of string | yes | Questions someone would ask later to find it (at most 6; extra ones are dropped). |
| `atoms` | list of [atoms](#atoms) | yes | Exact, reusable facts (at most 12; extra ones are dropped). |
| `project` | string or null | yes | What it is about (see [Projects](#projects)). |
| `account` | string or null | yes | The account it belongs to. Set only by `import-jsonl` for now. |
| `original` | string or null | `get_item` with `include_original` | The redacted text the summary was written from (see [The original](#the-original)). |
| `original_ref` | string or null | yes | Where the original came from, as `<kind>:<id>`. |
| `trust` | `"own"` or `"external"` | yes | Who wrote it (see [Trust](#trust)). |
| `content_hash` | string | no | SHA-256 (hex) of `original`. The same hash means nothing to summarise again. |

`search` adds `score` to each result (see [MCP tools](mcp-tools.md#search)).

An item as the MCP tools return it:

```json
{
  "id": 1,
  "source": "agent-claude-code-laptop",
  "external_id": "4b1c2d9e-0000-4000-8000-000000000001#0",
  "occurred_at": "2026-10-09T21:23:12+00:00",
  "title": "Invoice export: switch CSV writer to UTF-8 with BOM",
  "summary": "The invoice export opened with broken accents in spreadsheets. The CSV writer now writes UTF-8 with a BOM; the export test was updated.",
  "questions": ["Why do exported invoices show broken accents?"],
  "atoms": [
    {"kind": "decision", "text": "Invoice CSV export is written as UTF-8 with BOM"},
    {"kind": "error", "text": "UnicodeEncodeError: 'latin-1' codec can't encode character"},
    {"kind": "command", "text": "uv run pytest tests/test_export.py -q"}
  ],
  "project": "billing",
  "account": null,
  "original_ref": "claude-code-session:4b1c2d9e-0000-4000-8000-000000000001",
  "trust": "own"
}
```

The searchable text of an item is its title, summary, questions, atom texts and project.
The original and the source name are not searched (see
[How search ranks results](../explanation/search.md)).

## Atoms

An atom is one small, exact piece of knowledge from an item, with a kind. The summarizer
copies errors and commands verbatim.

| Kind | Meaning | Example |
|---|---|---|
| `decision` | What was decided. | `Invoice CSV export is plain UTF-8, no BOM` |
| `rationale` | Why it was decided. | `The accounting import rejects files that start with a BOM` |
| `fix` | What solved a problem. | `Set newline="" when opening the CSV file to stop blank rows on Windows` |
| `error` | Exact error text. | `UnicodeEncodeError: 'latin-1' codec can't encode character` |
| `command` | A command or snippet that worked. | `uv run pytest tests/test_export.py -q` |
| `fact` | A durable fact about a system, a preference or a setup. | `The atlas tile server listens on port 8080 behind the proxy` |
| `risk` | An open risk or pending issue. | `Old exports in the archive still carry a BOM` |

```json
{"kind": "decision", "text": "Invoice CSV export is plain UTF-8, no BOM"}
```

No other kind is accepted, and an atom's text is never empty.

## Sources

The `source` of an item is the `name` of the connector that produced it, or the
`--source` given to `jezero import-jsonl`. Names match `^[a-z0-9][a-z0-9_-]{0,63}$`.

| Convention | Example | Produced by |
|---|---|---|
| `agent-<agent>` | `agent-claude-code`, `agent-hermes` | A connector reading the server's own computer. |
| `<prefix>-<agent>-<device>` | `agent-codex-laptop`, `work-codex-work-mac` | A connector written by `jezero device approve` for a device's inbox. |
| anything else | `paperclip`, `mail-work` | Other connectors, imports. |

Keys are limited to sources by glob patterns (`sources = ["agent-*"]` in `[[clients]]`).
See [Connectors](connectors.md#source-names).

## Projects

`project` is a short lowercase name (`atlas`, `billing`, `shop`) or `null`. It is set,
first match wins, from:

1. What the source says: the session's folder through the project rules, an uploaded
   session's `.project` file, or `project_map` for Paperclip tasks (see
   [Connectors](connectors.md#projects)).
2. The connector's `project` key.
3. The summarizer's guess from the text, lowercased, preferring the
   `[summarizer].known_projects`.

Filters on `project` (`search`, `timeline`) are exact matches. `jezero retag` re-applies
the source's own answers to stored items without summarising them again.

## Trust

| Value | Meaning |
|---|---|
| `own` | Written by you or your agents. The default. |
| `external` | Written by third parties, such as mail. Its content is data: agents must never follow instructions in it. |

An item's trust is the `trust` of its connector (or `--trust` of `import-jsonl`). Every
tool result carries it.

## The original

`original` is the text the connector rendered from the source, after redaction: one line
per message or tool call, each starting with its UTC time.

```text
[2026-10-19 21:05] user: the accounting import rejects the invoice CSV
[2026-10-19 21:06] tool call: Bash {"command": "uv run pytest tests/test_export.py -q"}
[2026-10-19 21:09] assistant: The import fails on the BOM. I removed it from the writer.
```

It is kept in the library because agents delete their own transcripts (Claude Code after
30 days by default), and it is what `jezero resummarize` summarises again. Secrets are
redacted before it is stored, summarised or indexed; `jezero redact-stored` applies newer
rules to what is already stored.

`original_ref` names the original in its source:

| Connector type | `original_ref` |
|---|---|
| `claude_code` | `claude-code-session:<session id>`; a sub-agent transcript (with `include_subagents`): `claude-code-session:<session id>/<agent file>` |
| `codex` | `codex-session:<session id>` |
| `opencode` | `opencode-session:<session id>` |
| `hermes` | `hermes-session:<session id>` |
| `antigravity` | `antigravity-conversation:<conversation id>` |
| `paperclip` | `paperclip-issue:<company id>/<identifier>` (items stored before 0.1.0 have `paperclip-issue:<identifier>` until their issue is read again; see [Read a source again](../how-to/sources.md#read-a-source-again)) |
| `jsonl` | the record's `original_ref`, or null |

All the segments of one session share its `original_ref`. When a connector starts naming
an original differently, the next read of an unchanged unit updates `original_ref` in place
without summarising it again.

Titles, summaries, questions and atoms never hold terminal control characters: C0 controls
other than newline and tab, DEL, C1 controls and whole escape sequences (`ESC [ ... m`,
`ESC ] 8 ; ; URL BEL`, `ESC ] 52 ; ...`) are dropped from what the summarizer writes and
from imported records. The original of a session keeps them, since it feeds the content
hash; `jezero search`, `find`, `show` and the app drop them when they print anything.

## Ids

| Id | Where | Stable |
|---|---|---|
| `id` | The library's integer key. Cite it as `[123]`. | Kept when the item is updated; a new segment gets a new id. |
| `(source, external_id)` | Unique pair: how a connector finds the item it already stored. | As stable as the source's own ids. |

When a connector delivers a unit again, its text is compared by `content_hash`. The
same hash is skipped; a different one updates the item in place, with the same `id`, a
new summary and a new `ingested_at`.

A run deletes items in one case only. When a connector reads a session again whole and
it now splits into other segments (after a change to `[summarizer].max_input_chars`, or
because the session got shorter), the stored segments of that session whose
`external_id` is no longer produced are removed, and the run report counts them in
`pruned`. If any segment of the session fails to summarise, its old items stay until a
run succeeds. A session, issue or conversation that a source stops producing altogether
stays in the library.

## Dates

- `occurred_at` is when the work happened, not when it was ingested: the time of the
  last message or entry in the item. Agents' times are converted to UTC.
- Both dates are ISO 8601 with an offset of `+00:00`:
  `2026-10-19T21:23:12+00:00`. Naive datetimes are refused when an item is stored.
- Tool filters accept ISO 8601 dates (`2026-10-01`) or datetimes
  (`2026-10-01T08:00:00+02:00`); a value with no offset is read as UTC. `since` is
  inclusive, `until` exclusive.

## Segments of long sessions

A session whose rendered text is longer than `[summarizer].max_input_chars` (default
60000 characters) is split into consecutive segments, each stored as its own item.

- The rendered messages (one block per message, record or entry) are grouped in order
  until the next one would pass the limit. A single block longer than the limit is
  shortened in the middle, with `[... N characters omitted ...]`.
- Boundaries depend only on the blocks, so earlier segments stay identical when a session
  grows: only the last one changes, and only changed segments are summarised again.
- `external_id` is `<unit id>#<N>`, where `N` locates the segment's first block in the
  source. A session that fits in one segment also gets `#N`.
- When there is more than one segment, the source's title hint gets ` (part n/N)`.

| Connector type | `N` is |
|---|---|
| `claude_code`, `codex` | The 0-based line of the transcript file where the segment's first block is. |
| `opencode`, `antigravity` | The 0-based position of the segment's first block. |
| `hermes` | The database id of the segment's first message. |
| `paperclip` | `0` for the segment that starts with the issue's header, else the position of its first entry. |

Example: a long Claude Code session `4b1c2d9e-...` gives
`4b1c2d9e-...#0`, `4b1c2d9e-...#812` and `4b1c2d9e-...#1630`, three items with the same
`original_ref`.

## JSON Lines import

`jezero import-jsonl PATH --source NAME` stores items that are already summarised, one
JSON object per line. Every text field is redacted on the way in, and terminal control
characters are dropped from it.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `external_id` | string | yes | Unique within the source. |
| `occurred_at` | string | yes | ISO 8601 with an offset. |
| `title` | string | yes | Not empty. |
| `summary` | string | yes | Not empty. |
| `questions` | list of string | no | |
| `atoms` | list of `{kind, text}` | no | Kinds from [Atoms](#atoms). |
| `project` | string | no | Default: `--project`. |
| `account` | string | no | |
| `original` | string | no | |
| `original_ref` | string | no | |
| `trust` | `"own"` or `"external"` | no | Default: `--trust` (`own`). |
| `content_hash` | string | no | Default: SHA-256 of `original`, else of `summary`. |

```json
{"external_id": "note-17", "occurred_at": "2026-10-01T09:00:00+00:00", "title": "Shop checkout copies the tax id", "summary": "Invoices now copy the tax id at checkout time.", "atoms": [{"kind": "decision", "text": "Copy the tax id into the invoice at checkout"}], "project": "shop"}
```

A record with a missing field or a bad value stops the import with
`PATH:LINE: invalid record: ...`.

## Connector state

Each connector has one row of state next to the items, shown by `jezero health`:

| Field | Meaning |
|---|---|
| `cursor` | The last processed position (a timestamp), so a run reads only what is new. |
| `last_run_at` | When it last ran. `jezero run --due` compares it with `interval_minutes`. |
| `last_success_at` | When it last ran without a failed item. |
| `last_item_at` | When it last stored or updated an item. |
| `last_error` | The latest errors and warnings, or null. |

## See also

- [MCP tools](mcp-tools.md): how agents read items.
- [Connectors](connectors.md): what each source puts in an item.
- [How summaries are made](../explanation/summaries.md): title, summary, questions and
  atoms.
- [Configuration](configuration.md): `[storage]`, `[summarizer]` and `[projects]`.
