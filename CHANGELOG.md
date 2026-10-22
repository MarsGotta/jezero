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
- Hermes Agent plugin (`plugins/hermes`, `hermes plugins install MarsGotta/jezero/plugins/hermes`):
  `jezero_*` tools, related past work on the first turn, the skill, and upload of the
  session as turns end. The `hermes` connector reads those uploads with `export_dir`, and
  takes the project from the session's working folder.
- OpenCode plugin is a self-contained folder (`plugins/opencode`, with its core, skill and
  `package.json`): related past work after the first prompt, the skill installed in
  OpenCode's skills folder.
- `init --no-upload` for computers that only read Jezero (the server's own computer).
- Client 0.3.1: `status` on a read-only computer checks reading, not uploading.
- Documentation rewritten and organised by Diátaxis in `docs/`: two tutorials, seven
  how-to guides, seven reference pages, five explanation pages, a glossary and a style
  guide. The command and configuration references are generated from the code
  (`scripts/make_reference.py`); `scripts/check_docs.py` checks every link and anchor.
  Both run in CI.
- The app's Install and Update buttons run the full documented steps for each agent
  (plugin, MCP server), not only `setup`; the package ships the OpenCode plugin for it.
- The Jezero app: `jezero` with no command opens a terminal app (Textual) with Home,
  Search (with "newer on this subject"), Timeline, Agents (detect, install and update the
  plugins on this computer) and Agent view (the exact context an agent receives). Night
  and dawn themes; `jezero ui --demo` runs it on made-up data. People commands:
  `jezero connect`, `find`, `show --newer`, `agents`. New `GET /api/v1/overview` (any
  client key, only its sources) feeds the dashboard without adding an MCP tool. The
  client core ships inside the package as `jezero/client_core.py` (generated).
- Antigravity plugin (`plugins/antigravity`, client 0.4.0): the MCP server through a local
  stdio bridge (`jezero_client.py mcp`, so the key never sits in Antigravity's config),
  the protocol and recent work at SessionStart, related past work before the first model
  call, the skill, and upload of `transcript.jsonl` when a conversation stops. The
  `antigravity` connector reads those uploads with `transcripts_dir`. The key may live in
  `~/.gemini/config/jezero` for sandboxes that cannot see the user's home.
- Client 0.3.3: the OpenCode plugin also finds a key in `<OpenCode config>/jezero`.
- Client 0.3.2: sandboxed agents keep their own key in `<agent config>/jezero`
  (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`), used when `JEZERO_CLIENT_HOME` is not set.
- `since`/`until` accept a bare date or a datetime without offset (read as UTC).
- Connectors register themselves: each module declares a `CONNECTOR_TYPE` with its
  `build` function and is discovered automatically; other packages can add connectors
  through the `jezero.connectors` entry point group. Adding a connector is one file.
- Summariser tests against a fake endpoint (both APIs, retries, errors, loose output).
- Tidy-up: the old `project_from_cwd` helper and an empty test file are gone; the diagram
  script moved to `scripts/`; `plugins/` carries a do-not-edit note.
- The plugins install straight from GitHub: `claude plugin marketplace add MarsGotta/jezero`
  and `codex plugin marketplace add MarsGotta/jezero`. `plugins/` is generated by
  `scripts/build_clients.py` and checked in CI.
- A visual README: architecture diagram (`docs/architecture.svg`, drawn by
  `scripts/make_architecture.py`), badges and a synthetic example of an agent using Jezero.
- Open-source readiness: CI workflow (lint, tests, privacy check, Python 3.9 client
  check), `docs/architecture.md`, plugin manifests under Apache-2.0 like the project, the
  obsolete `receiver` connector type removed (the device inbox replaces it), redaction no
  longer hides code such as `token = get_access_token()`, neutral names in tests.
- Project detection without assumptions about folder names (`jezero.projects`, shared
  by the connectors and the device client): configured `[projects] paths` first, then the
  git repository (the main repository for worktrees), then common root folders
  (`projects`, `code`, `dev`, `src`, `repos`... configurable, case-insensitive). Devices
  send the project they found with each upload (`X-Jezero-Project`), stored next to the
  session as `<file>.project`.
- Stronger redaction: env-style names ending in a secret word (`GITHUB_TOKEN=`,
  `PGPASSWORD=`, `db_pass=`), quoted JSON keys, `--password X` flags, Basic auth, GitLab,
  npm, Hugging Face and Stripe tokens, Azure keys and signed URLs. References (`$VAR`,
  `<placeholder>`, paths) and prose (`tests PASS`, `OAuth`, `persist-credentials`) are kept.
  `jezero redact-stored [--dry-run]` applies the rules to items already stored.
- `jezero device approve --source-prefix/--read-only`.
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
- Client 0.5.0:
  - The Claude Code plugin brings its own MCP server (`.mcp.json`, the stdio bridge), and
    `setup --agent claude-code` / `--agent codex` register the stdio bridge instead of
    writing the bearer key in clear into the agent's config. With the Claude Code plugin
    installed, `setup` registers nothing and removes an old user-scope `jezero` HTTP entry;
    an old `[mcp_servers.jezero]` HTTP block in Codex' `config.toml` is replaced, the rest
    of the file left byte for byte.
  - `client.json` takes an optional `mcp_path` (default `/mcp`, set with
    `init --mcp-path`) for servers with a custom `[server].path`; the core and the
    OpenCode plugin use it.
  - Uploads refused with HTTP 401/403 stay queued (logged once) until the key may upload;
    files rejected with 400/413/415 are recorded as rejected, not as uploaded, and
    `status` reports them with the queue size.
  - A key kept in an agent's own folder (Antigravity's `~/.gemini/config/jezero`,
    `$CLAUDE_CONFIG_DIR/jezero`, `$CODEX_HOME/jezero`) is used by the background upload,
    `backfill --agent` and `setup --agent`, and by the `mcp` bridge for every agent.
  - Without a key the session-start hooks print nothing; the Hermes plugin gives the
    protocol once per session. `state.json` no longer grows without limit. `flush`
    always prints its counts; `status` and `flush` take `--agent`.
  - The app labels newer items found only by date: "NEWER IN THIS PROJECT (by date, not
    by subject)".
