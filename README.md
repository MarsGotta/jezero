# Jezero

**A shared, searchable library of your agents' past work.**

Your agents forget. Their sessions pile up as gigabytes of transcripts on several
machines, and your email holds decisions nobody wrote down anywhere else. Jezero
collects those sources, turns each session or thread into a short summary plus
**atoms** (decisions, rationale, fixes, exact errors, commands that worked, facts,
risks), and serves it all, read-only, to every agent you run over
[MCP](https://modelcontextprotocol.io).

*The name:* Jezero is the Martian crater that once held a lake. Its layered sediments
keep the record of what happened there, and the Perseverance rover sets samples aside in
it so they can be picked up later. That is the whole idea of this project.

```
 sources          connectors         pipeline                    store            access
 agent sessions ─┐                                                                ┌─ agent A
 other devices  ─┼─> incremental ──> redact secrets ──> summary ──> SQLite ──> MCP ┼─ agent B
 email (opt-in) ─┘   read-only       mark external      questions   + FTS5   read- └─ ...
                     hashed          content            atoms       one file only
```

**Status: pre-alpha.** Storage, search, the MCP server, configuration, the summarizer,
connectors for Hermes Agent, Claude Code and Codex, unattended runs and the watchdog are
implemented and tested. Sessions from other devices and email come next.
`jezero check-config` tells you which connector types are available in your version.

## Why this design

- **Write once, read many.** The expensive step (a cheap LLM summarising a session) runs
  once per item. Reads are plain full-text search: milliseconds, no GPU, no LLM, so ten
  or more agents can query at the same time.
- **Keyword search over summaries, not embeddings over raw text.** Transcripts are mostly
  tool output. Summaries plus "questions that would find this" make BM25 work well and
  keep the index small. Vector search can be added later in the same file
  ([sqlite-vec](https://github.com/asg017/sqlite-vec)) if keyword search falls short.
- **One SQLite file.** No database server to run or upgrade. WAL mode lets many readers
  work while a connector writes. Backups are a file copy.
- **Recency matters.** Results are boosted when recent (configurable half-life), so an
  old price or an abandoned decision does not outrank the current one.
- **Separate write and read paths.** Connectors only write to the library and only read
  from your sources. Agents only read. There is no write tool over MCP.

## Quick start

Requires Python 3.12+, SQLite 3.45+ (bundled with recent Python builds) and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp jezero.example.toml jezero.toml   # then edit it
uv run jezero check-config
uv run jezero init

# Try it with the synthetic sample items
uv run jezero import-jsonl examples/items.example.jsonl --source demo
uv run jezero search "migration rollback"

# Serve over MCP (Streamable HTTP)
uv run jezero new-token   # a token for the client, a digest for the config
uv run jezero serve
```

Connect a client, for example Claude Code:

```bash
claude mcp add --transport http jezero http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

## MCP tools

| Tool | What it does |
|---|---|
| `search` | BM25 over titles, summaries, questions and atoms, boosted by recency. Filters: `project`, `source`, `since`. |
| `get_item` | One item in full; the original text only on request. |
| `timeline` | Items in reverse chronological order for a project, source or period. |
| `list_sources` | Sources visible to the calling client, with counts and latest date. |

All tools are annotated read-only. Each client sees only the sources its config entry
allows (glob patterns), so a coding agent does not have to see your mail.

## Configuration

Everything lives in one TOML file: storage, search tuning, summarizer endpoint, server
(host, port, public URL behind a proxy), clients and their allowed sources, connectors,
redaction and the watchdog. [`jezero.example.toml`](jezero.example.toml)
documents every field.

**Secrets never go in the config.** Fields ending in `_env` hold the *name* of an
environment variable. Inline secrets in connector options are rejected at load time.
A git-ignored `.env` file next to the config is loaded automatically; variables already
set in the environment take precedence. `check-config` reports which variables are set
without printing their values.

## Security model

- A bearer token per client, compared by SHA-256 digest in constant time.
- Unauthenticated mode is refused unless the server binds to a loopback address.
- DNS-rebinding protection on the HTTP transport (Host and Origin checks).
- Secrets are redacted before anything is summarised, stored or indexed.
- Content written by third parties (email) is stored with `trust = "external"`, and the
  server instructions tell agents to treat it as data, never as instructions.
- Search queries are always quoted before reaching FTS5, so they cannot inject operators.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests
git config core.hooksPath .githooks   # enables the privacy pre-commit check
python3 scripts/privacy_check.py --all
```

The pre-commit hook blocks commits that contain absolute home paths, private network
addresses, credential shapes, or any term listed in your private, git-ignored
`.privacy-denylist`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Running it unattended

`jezero run --due` runs the connectors whose interval has elapsed, `jezero health` flags
connectors that went quiet. Ready-made systemd user units and a cron line are in
[`deploy/`](deploy/README.md).

## Roadmap

1. A receiver plus small emitters for sessions on other devices.
2. Gmail connector (read-only scope, per thread, newsletters skipped).
3. A retrieval benchmark command (questions with expected hits, top-k accuracy).
4. Optional vector search with sqlite-vec.

## License

[Apache-2.0](LICENSE)
