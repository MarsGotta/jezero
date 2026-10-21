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
uv run ruff check src tests scripts clients
uv run ruff format --check src tests scripts clients
python3 scripts/privacy_check.py --all
python3 scripts/build_clients.py && uv run --no-project --python 3.9 python dist/client/jezero_client.py --version
python3 scripts/build_clients.py --check   # plugins/ is generated from clients/, never edit it
uv run python scripts/make_reference.py --check   # docs/reference/{cli,configuration}.md
uv run python scripts/check_docs.py               # links and anchors in every .md
```

## Layout

- `src/jezero/config.py`: TOML config and validation.
- `src/jezero/connectors/`: one module per source type; each declares its
  `CONNECTOR_TYPE` and is discovered automatically (`__init__.py`, see CONTRIBUTING).
- `src/jezero/projects.py`: project detection, shared with the device client (stdlib,
  Python 3.9).
- `src/jezero/redact.py`: secret redaction.
- `src/jezero/summarize.py`: the summariser (Anthropic or OpenAI-compatible APIs).
- `src/jezero/pipeline.py`: collect, redact, summarise, store for one connector.
- `src/jezero/store.py`: SQLite schema, upserts, BM25 + recency search.
- `src/jezero/server.py`: MCP server, bearer auth, per-client source filtering, device
  inbox routes; `inbox.py` stores uploads.
- `src/jezero/watchdog.py`, `cli.py`, `ingest.py`: health checks, commands, JSONL import.
- `clients/`: device client and plugins (the source). `plugins/` and the two
  `marketplace.json` files are generated from it by `scripts/build_clients.py`.
- `scripts/`: build, privacy check, architecture diagram. `deploy/`: systemd units.
- `tests/`: unit tests and end-to-end tests over real HTTP.
