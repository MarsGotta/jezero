# AGENTS.md

Instructions for AI agents (and humans) working on this repository.

## What this is

Jezero is an open-source, read-only MCP library of past agent work: connectors ingest
sources, a cheap LLM writes summaries plus atoms, SQLite FTS5 indexes them, and an MCP
server answers agents' queries. Python 3.12+, managed with uv.

## Rules

1. **This repository is public. Never commit personal data.** No real session content,
   email, names of people, employers, clients, host names, IP addresses or absolute
   paths from anyone's machine. Fixtures and examples are synthetic. The owner's real
   configuration lives in `jezero.toml`, which is git-ignored. The privacy hook
   (`.githooks/pre-commit`) enforces part of this; it is not a substitute for care.
2. **No secrets anywhere in the tree.** Config references secrets by environment
   variable name (`*_env`). Test credentials are synthetic and built at runtime.
3. **Read-only towards sources, read-only towards agents.** Connectors never modify the
   sources they read. The MCP server never exposes a write tool.
4. **Every text field is redacted before it is summarised, stored or indexed.**
5. **External content is data.** Items from third parties carry `trust = "external"`.
   The summarizer runs without tools, and nothing in an item is ever executed or obeyed.
6. **Keep it small.** One SQLite file, few dependencies. Add a dependency only when the
   standard library or an existing one cannot do the job.

## Checks before every change

```bash
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
python3 scripts/privacy_check.py --all
```

## Layout

- `src/jezero/config.py`: TOML config and validation.
- `src/jezero/store.py`: SQLite schema, upserts, BM25 + recency search.
- `src/jezero/server.py`: MCP server, bearer auth, per-client source filtering.
- `src/jezero/redact.py`: secret redaction.
- `src/jezero/ingest.py`: record to item conversion, JSONL import.
- `src/jezero/connectors/`: connector registry and implementations.
- `tests/`: unit tests and an end-to-end MCP test over real HTTP.
