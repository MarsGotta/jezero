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
