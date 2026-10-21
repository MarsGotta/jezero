# Design decisions

The main choices behind Jezero, in a short decision-record form: the context, what was
decided, what follows from it, and the alternatives. The reasons come from the README, the
changelog and comments in the code. Where the project does not record a reason, the entry
says so and describes the trade-off as it shows up in use.

| Decision | In one line |
|---|---|
| [One SQLite file with FTS5](#one-sqlite-file-with-fts5) | no database server, backups are a file copy |
| [Keyword search over summaries, not vectors](#keyword-search-over-summaries-not-vectors) | literal, fast, explainable; synonyms are the cost |
| [Summaries and atoms, written once by a cheap model](#summaries-and-atoms-written-once-by-a-cheap-model) | pay once per session, never per read |
| [Recency as a bounded boost](#recency-as-a-bounded-boost) | newer wins ties, old knowledge stays findable |
| [`later` instead of superseding](#later-instead-of-superseding) | the library never rewrites an item; agents check for newer ones |
| [A read-only MCP server](#a-read-only-mcp-server) | agents cannot change memory |
| [A separate inbox for uploads](#a-separate-inbox-for-uploads) | one write path, outside MCP, reusing the connectors |
| [Device keys created on the device](#device-keys-created-on-the-device) | the server only ever stores a digest |
| [One standard-library client core, copied into each plugin](#one-standard-library-client-core-copied-into-each-plugin) | runs on any computer's python3, installs per agent |
| [Hooks, so agents use Jezero by habit](#hooks-so-agents-use-jezero-by-habit) | context arrives without being asked for |
| [The source's project beats the model's](#the-sources-project-beats-the-models) | folders say where work happened |
| [Summarise finished sessions, in stable segments](#summarise-finished-sessions-in-stable-segments) | no repeated calls on a growing session |
| [Secrets only by environment variable name](#secrets-only-by-environment-variable-name) | the config can be shared |
| [The overview endpoint outside MCP](#the-overview-endpoint-outside-mcp) | one less tool for agents to choose from |
| [A terminal app built with Textual](#a-terminal-app-built-with-textual) | people look through the same door as agents |
| [Connectors register themselves](#connectors-register-themselves) | a new source is one file |
| [Unattended runs that fail loudly](#unattended-runs-that-fail-loudly) | one run at a time, silence is an alert |

## One SQLite file with FTS5

**Context.** The library is written by a few connectors on one server and read by many
agents at once. It holds thousands to tens of thousands of short records.

**Decision.** Store everything in one SQLite file: the items, an FTS5 full-text index kept
in sync in the same transaction, and the connectors' state. Use WAL mode so readers work
while a connector writes.

**Consequences.** There is no database server to run, secure or upgrade, and a backup is a
file copy. Writers are serialised by SQLite, which suits a pipeline that runs one
connector run at a time. The index needs SQLite 3.45 or newer for accent-insensitive
trigrams. Schema migrations are not implemented yet: `init` refuses an unknown schema
version and a tokenizer different from the one the file was built with. Following the
project's rule "keep it small", the store adds no dependency.

**Alternatives.** A database server (PostgreSQL with full-text search) would add a
service to operate for a load SQLite handles. A dedicated vector database would add one
too, for a kind of search Jezero does not use (next entry).

## Keyword search over summaries, not vectors

**Context.** Agents look for exact things: an error, a command, a service name, a past
decision about a named component. The text available for each session is a summary and
atoms written for retrieval.

**Decision.** Rank with BM25 over the summary fields, using a trigram tokenizer that
ignores case and accents and matches substrings. Quote every query term, so queries are
literal and cannot inject operators. The full mechanics are in
[How search ranks results](search.md).

**Consequences.** Reads take milliseconds with no GPU and no model, so ten or more agents
can query at once, and a hit can be explained by the words it contains. Plurals and word
fragments match through substrings. A query in different words from the item misses it;
that cost is reduced by the summariser's "questions that would find this", by
`summarizer.output_language`, and by telling agents to retry with synonyms or the
singular. Terms under three characters cannot be searched on their own.

**Alternatives.** Embeddings over raw transcripts were rejected in the README's terms:
transcripts are mostly tool output, and summaries plus questions make BM25 work well while
keeping the index small. Vector search is deferred, not ruled out: it can be added later
in the same file with sqlite-vec if keyword search falls short, and a retrieval benchmark
is on the roadmap to tell.

## Summaries and atoms, written once by a cheap model

**Context.** Sessions are long, noisy and numerous. An agent that finds one needs the
outcome, not the transcript.

**Decision.** One LLM call per session (or segment) writes a title, a summary, questions,
up to twelve typed atoms and a project slug, through a fixed JSON schema. Use a cheap,
fast model, on any Anthropic or OpenAI-compatible endpoint. Keep the redacted original
next to the item. See [Summaries and atoms](summaries.md).

**Consequences.** The expensive step runs once per item and never on reads ("write once,
read many"). Unchanged text is never summarised twice, thanks to a content hash. The
library's quality depends on the summariser: a missed decision is not in the atoms, though
it may still be in the original. Changing the model or the language means re-summarising
on purpose (`jezero resummarize`). The provider sees the redacted text of every session.

**Alternatives.** Indexing raw transcripts (see above). A stronger, more expensive model
would write better summaries at a price paid on every session of every agent, including
backfills of thousands; the config steers towards a cheap one. Summarising on each device
would put a model key on every computer and duplicate the pipeline; the inbox keeps it in
one place.

## Recency as a bounded boost

**Context.** An old price or an abandoned decision should not outrank the current one.
But old knowledge, such as how an obscure error was fixed, must stay findable.

**Decision.** Re-rank the best BM25 candidates by a factor between `1 - recency_weight`
and 1 that halves its distance from the floor every `recency_half_life_days`
(0.3 and 90 days by default).

**Consequences.** Recency reorders relevant items and never makes an irrelevant one
relevant. An old item still wins when it is clearly more relevant (about 1.43 times, with
the defaults). Only the candidate pool is re-ranked, so very recent but weak matches can
stay out. Recency cannot tell whether a newer item changed an older one, which is why
`later` exists.

**Alternatives.** Sorting by date, or filtering to recent items, would hide old fixes.
Pure BM25 would let stale decisions win on wording alone.

## `later` instead of superseding

**Context.** Decisions change. Each item summarises one session, so nothing in the
library marks a decision as superseded: the session that reversed it is a separate item.

**Decision.** Add the MCP tool `later(item_id, query?)`: newer items of the same project on
the same subject, newest first, with a flag saying whether they matched the subject or are
the project's timeline as a fallback. The server instructions and the plugin protocol tell
agents to call it before answering with a decision, plan, status or number. The matching
rules are in
[How search ranks results](search.md#how-later-finds-newer-work-on-the-same-subject).

**Consequences.** Items are immutable history; the answer about the current state is
assembled at read time, and the agent can say when and where something changed. It costs
one more call, and it depends on agents following the instruction. The project is the
boundary: a change recorded under another project is not found.

**Alternatives.** Rewriting or merging old items when a newer session contradicts them
would need a write path from reading agents (or a model judging contradictions at ingest
time), and would lose the history of what was believed when.

## A read-only MCP server

**Context.** Agents read untrusted content and can be manipulated by it. A shared memory
that agents can write is a shared memory that one fooled agent can poison for all the
others.

**Decision.** Expose no write tool. Annotate every tool read-only, open the database
read-only for every request, and let only connectors, run by the admin, write items. The
plugin protocol tells agents there is nothing to save.

**Consequences.** The worst a manipulated agent can do through Jezero is read what its key
allows. Memory grows only from real sessions, and agents cannot record a note on purpose:
what they did is captured when the session is ingested. See
[Security model](security.md#the-mcp-server-is-read-only).

**Alternatives.** A `save` or `remember` tool, common in agent memory servers, was left
out for the reason above.

## A separate inbox for uploads

**Context.** Sessions happen on several computers, and the connectors run on the server.
The read-only MCP channel cannot carry them.

**Decision.** Add `PUT /inbox/v1/<agent>/<path>`, a plain HTTP route for keys with
`upload = true`. Store each file as sent, in `<inbox>/<device>/<agent>/...`, mirroring the
agent's own store, and let the usual connectors read those folders. Set each file's
modification time to the upload time.

**Consequences.** Summarising, redaction and deduplication stay in one place: a device's
folder is read by the same connector code as a local store. No SSH access between
computers is needed. A key can write only into its own device folder, and only paths that
match its agents' layouts. Using the upload time keeps a device that was offline for a
week from uploading "into the past" behind a connector's cursor. Raw transcripts are kept
on the server, unredacted. An earlier `receiver` connector type was removed when the inbox
replaced it.

**Alternatives.** Syncing agent folders over SSH, which the README explicitly avoids. A
write tool in MCP, which would break the previous decision.

## Device keys created on the device

**Context.** Each computer needs its own key, and the admin of the server needs to approve
it. Sending a secret through chat or email to whoever installs a plugin is the usual weak
point.

**Decision.** `jezero_client.py init` creates the key on the device and prints only its
SHA-256 digest, as a ready-made `jezero device approve` line. The server stores the
digest; the approval also creates the device's inbox connectors, disabled.

**Consequences.** Nothing secret passes through the approval, and a copy of the server's
config holds no usable key. Revoking is removing an entry and restarting. Sandboxed agents
can hold their own key in their config folder. A lost device key cannot be recovered; the
device makes a new one.

**Alternatives.** Admin-generated tokens remain available for other clients
(`jezero new-token` prints the token once and its digest for the config), but for devices
they would mean handing a secret over.

## One standard-library client core, copied into each plugin

**Context.** Plugins run on computers Jezero does not control, inside five different
agents, each with its own plugin system and install path.

**Decision.** Put all client logic in one file, `clients/core/jezero_client.py`: Python 3.9
or newer, standard library only, so it runs with the `python3` that ships with macOS.
Plugins are thin adapters that map their agent's events to commands of that file.
`scripts/build_clients.py` copies the core, the project rules and the shared skill into
every plugin, and CI checks that `plugins/` matches `clients/`.

**Consequences.** Nothing to install on a device besides the plugin; each plugin folder
is complete and installs straight from GitHub through its agent's own mechanism. There is
a single source for the logic, but several copies are installed, so a computer can run
different core versions in different agents (the app's Agents screen shows them). The
standard-library constraint rules out conveniences such as an HTTP client library. The
server package ships the same core as `jezero/client_core.py` for the app.

**Alternatives.** A pip-installed client package would need a Python environment on every
device. Plugin-specific implementations would multiply the logic by five.

## Hooks, so agents use Jezero by habit

**Context.** An MCP tool that an agent is never prompted to use is rarely used. People do
not mention Jezero in their prompts, and should not have to.

**Decision.** The plugins hook into agent events. At session start they add a short
protocol (search before non-trivial work, check `later`, cite ids, never guess, nothing to
save) and the project's five most recent items. On the first prompt they add up to three
related items, chosen by a two-word rule rather than a score
([why](search.md#context-on-the-first-prompt)). Hooks never block or break the agent:
2.5-second timeouts, a catch-all, uploads in a detached process.

**Consequences.** Agents use the library without being asked, as in the README's example.
The cost is a little context in every session and two calls to the server at its start.
The rule for related items is strict on purpose, so the block stays rare enough to be
read. When the server is down, sessions lose the context and nothing else.

**Alternatives.** Relying only on the MCP server's instructions and tool descriptions.
Those exist too, but they wait for the agent to decide to use a tool; the hooks put the
protocol and the context into the conversation itself.

## The source's project beats the model's

**Context.** `timeline`, `later` and the session-start context all work per project. The
model can propose a project, but its guess follows whatever the text mentions.

**Decision.** A hint from the source wins: the session's working folder (configured paths,
then the git repository, the main one for worktrees, then common root folders) or the
project a device found and sent with the upload. The same project rules run on the server
and on devices. `jezero retag` re-applies hints to stored items without a model.

**Consequences.** Items land in the project where the work happened, even when a session
talks about another one. A session run outside any recognisable folder falls back to the
model's guess, or the connector's default project.

**Alternatives.** Trusting the model's tag, which was the first behaviour and the reason
for the change.

## Summarise finished sessions, in stable segments

**Context.** Sessions grow for hours or days, and a long one does not fit one cheap call.

**Decision.** Defer a session until it has been quiet for `settle_minutes` (30). Cut long
sessions into segments of at most `max_input_chars` (60,000) whose boundaries depend only
on the blocks themselves, with ids built from the session and the segment's first block.

**Consequences.** An ongoing conversation is not summarised every few minutes, and as a
session grows only its last segment changes and is summarised again. A finished session
reaches the library within about an hour. A long session becomes several items.
See [segmenting](summaries.md#long-sessions-are-cut-into-segments).

**Alternatives.** Summarising a whole session at once, which is impossible above the
input limit and would be repeated in full on every change.

## Secrets only by environment variable name

**Context.** The config describes machines, sources and clients. It should be possible to
share it, review it and keep it in version control.

**Decision.** Fields ending in `_env` hold the name of an environment variable; connector
options that look like secrets are rejected; client keys appear only as digests. A
`.env` file next to the config is loaded, and the environment wins over it.

**Consequences.** A config file leaks nothing. `check-config` can tell which variables are
missing without printing values. Setting up a service means providing the variables, for
example with a systemd `EnvironmentFile`.

**Alternatives.** Inline secrets with a "do not commit" warning, which is how leaks
usually start.

## The overview endpoint outside MCP

**Context.** The app's home screen needs counts per source, per project and per day.

**Decision.** Serve them at `GET /api/v1/overview`, for any client key and only its
sources, instead of adding an MCP tool. The code states the reason: agents do not get one
more tool to choose from.

**Consequences.** The MCP surface stays at five tools, all useful to an agent. The app
uses two interfaces, MCP and one HTTP route, with the same key.

**Alternatives.** An `overview` MCP tool, rejected for the reason above.

## A terminal app built with Textual

**Context.** People also want to look: what Jezero holds, whether each source is alive,
what an agent receives, and whether the plugins on this computer are up to date.

**Decision.** `jezero` with no command opens a terminal app built with Textual, with Home,
Search, Timeline, Agents and Agent view screens. It reads through the same MCP tools and
key as an agent, plus the overview endpoint; `jezero ui --demo` runs it on made-up data.

**Consequences.** The project does not record why a terminal app was chosen. As it shows
up in use: the app runs wherever the `jezero` command is installed, over SSH included, and
needs no web server, browser session or extra port to secure. Because it reads like an
agent, what people see is what agents can see, limited by the same source patterns. The
cost is two dependencies (Textual and Rich) in a project that keeps dependencies few, and
a terminal is a narrower canvas than a web page.

**Alternatives.** A web dashboard served by `jezero serve` is the obvious one; it would
widen the HTTP surface that the security model keeps small.

## Connectors register themselves

**Context.** Every new source used to mean editing a central list.

**Decision.** Each connector module declares a `CONNECTOR_TYPE` with its `build` function
and is discovered automatically; other packages can add connectors through the
`jezero.connectors` entry point group. Planned types are listed without a `build`, so
`check-config` can tell a typo from a type that is not implemented yet.

**Consequences.** Adding a connector is one file. A connector only knows its source's
format; redaction, hashing, summarising and cursors stay in the pipeline. See
[Add a source](../how-to/sources.md).

**Alternatives.** A hard-coded registry, which was the earlier state.

## Unattended runs that fail loudly

**Context.** Jezero runs from a timer, and a connector that quietly stops bringing anything
in looks exactly like a quiet week.

**Decision.** `jezero run --due` runs only connectors whose interval has elapsed, one run
at a time (a lock file), and a failing connector no longer stops the others. Connectors
can report warnings, for example when a source format changes; the pipeline records them
as the connector's error and holds the cursor. `jezero health` flags stale and failing
connectors, can post a webhook alert with de-duplication, and can ping a push monitor on
every check, so that silence itself becomes an alert.

**Consequences.** A problem stays visible until it is fixed, instead of a cursor moving
past data that was not read. A dead timer is caught by the monitor on the other end, not
by Jezero. See [Monitor Jezero](../how-to/monitor.md).

**Alternatives.** Logging errors and moving on, which hides format changes until someone
notices missing memories.

## See also

- [Architecture](architecture.md): how these decisions fit together.
- [Security model](security.md): the decisions seen from the attacker's side.
- [CHANGELOG.md](../../CHANGELOG.md): the record these entries are drawn from.
