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
- Claude Code and Codex connectors.
- `jezero run --due` (per-connector intervals, one run at a time, a failing connector
  no longer stops the others) and `jezero health` (stale/failing connectors, optional
  webhook alert with de-duplication). systemd user units in `deploy/`.
- Renamed from "biblioteca" to Jezero.
- Codex connector: `sessions_dir` accepts a glob (one connector for every agent home an
  orchestrator creates) and `user_prompt_markers` / `user_prompt_drop_lines` strip the
  agent instructions an orchestrator repeats in every prompt.
- Paperclip connector: each issue with its description, comments (system notices and
  deleted comments skipped), documents and answered questions, read through the API with
  GET requests only. Entries are ordered by creation time so segments stay stable as an
  issue grows; quiet issues only.
- OpenCode connector: sessions from `opencode.db` opened read-only; dialogue plus one
  line per shell command or edited file, reasoning and read-only tools skipped.
- `summarizer.output_language`: write every summary in one language, so a query in that
  language also finds sessions held in another.
- Prompt cleaning (`user_prompt_markers`, `user_prompt_drop_lines`) shared by connectors.
- A project hint from the source (working directory, project field) now wins over the
  model's guess. `jezero retag --connector NAME [--dry-run]` re-applies the hints to items
  already stored, without calling the model.
