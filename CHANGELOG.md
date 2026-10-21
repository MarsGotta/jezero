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
- `jezero resummarize --ids ... | --ids-file FILE`: summarise stored items again from
  their kept original text (for example after setting `summarizer.output_language`).
- Antigravity CLI connector: reads the per-conversation SQLite store through a minimal
  protobuf wire reader (user text, agent replies, one line per tool call with its summary);
  unknown or malformed steps are skipped.
- Connectors can report warnings. The pipeline stores them as the connector's error and
  holds the cursor, so `jezero health` (and the heartbeat) shows the connector as failing
  until the cause is fixed. The Antigravity connector warns when a conversation has steps
  but nothing in it can be read, which is how a format change shows up.
- MCP tool `later(item_id, query?)`: newer items in the same project on the same subject,
  newest first, flagged `matched_by=subject` or `project_timeline` (fallback). The server
  instructions now tell agents that decisions change and to check `later` before answering
  with a decision, plan or status. Each item summarises one session, so nothing else marks
  a decision as superseded.
- Plugins for Claude Code, Codex and OpenCode (`clients/`), thin adapters over one shared
  core (`clients/core/jezero_client.py`, Python 3.9+, standard library only). They add a
  short protocol and the project's recent work at session start, related past work on the
  first prompt, and upload each session to the server when it ends. The device key is
  created on the device; the server only stores its SHA-256 (`jezero device approve`).
  `scripts/build_clients.py` builds the bundle served at `/client/jezero-client.tar.gz`.
- Inbox: `PUT /inbox/v1/<agent>/<path>` for clients with `upload = true`; files land in
  `<inbox>/<device>/<agent>/...`, mirroring each agent's store, and the usual connectors
  read them. Codex rollouts compressed with zstd (`.jsonl.zst`) are read; OpenCode can
  read session exports (`export_dir`); Claude Code can skip project folders
  (`skip_project_dirs`).
- The MCP server unit stops within 10 s: clients keep streams open and the default 90 s
  wait made restarts slow.
