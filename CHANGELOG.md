# Changelog

## Unreleased

- Project skeleton: configuration file with validation and env-only secrets.
- SQLite + FTS5 store (trigram tokenizer, accent-insensitive) with BM25 ranking and a
  recency boost.
- Read-only MCP server over Streamable HTTP with per-client bearer tokens and source
  filtering: `search`, `get_item`, `timeline`, `list_sources`.
- Secret redaction before storage.
- JSONL import of pre-summarised items.
- Privacy pre-commit check.
- Summarizer for Anthropic and OpenAI-compatible endpoints: title, summary, questions,
  atoms and project through a fixed JSON schema, with retries.
- Hermes Agent connector: read-only, dialogue only (tool results skipped), injected
  memory blocks stripped, stable segments for long sessions, active sessions deferred.
- `jezero run` with `--dry-run`, `--limit` and `--sample`; per-connector cursor.
- A `.env` file next to the config is loaded automatically.
