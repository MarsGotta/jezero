# Your first library

In about fifteen minutes you install Jezero on your computer, fill a library with sample
items, search it, serve it over MCP and look at it in the Jezero app. Nothing here needs an
API key or a second computer.

## Before you start

You need:

- [git](https://git-scm.com), [curl](https://curl.se) and Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/), which installs Jezero and its dependencies.
- Port 8765 free on this computer. If something already uses it, pick another port in
  step 2 and use it everywhere this page says `8765`.

Every command runs in a terminal, from the folder you clone in step 1, unless a step says
otherwise.

## 1. Install Jezero

Clone the repository and install it with uv:

```bash
git clone https://github.com/MarsGotta/jezero.git
cd jezero
uv sync
uv run jezero --version
```

You should see:

```text
jezero 0.1.0.dev0
```

`uv run` runs a command inside the project's own environment, so nothing is installed
system-wide.

## 2. Write a config

Jezero reads one TOML file, `jezero.toml`, from the current folder. The repository ships
[`jezero.example.toml`](../../jezero.example.toml), which documents every field and points
at sources you may not have yet. For this tutorial a small file is enough.

Create `jezero.toml` in the `jezero` folder with this content:

```toml
# A local library for the tutorial. jezero.example.toml documents every field.
[storage]
path = "data/jezero.db"

[server]
host = "127.0.0.1"
port = 8765
require_auth = false
```

`require_auth = false` lets anything on this computer read the library without a key.
Jezero accepts it only when the server listens on a loopback address such as `127.0.0.1`.
You turn it on in step 7.

Check the file:

```bash
uv run jezero check-config
```

You should see:

```text
config: /home/me/jezero/jezero.toml
database: /home/me/jezero/data/jezero.db
sqlite: 3.53.1  tokenizer: trigram remove_diacritics 1
server: http://127.0.0.1:8765/mcp
auth: OFF (loopback only)
summarizer: anthropic model=NOT SET
clients:
connectors:
environment variables (values never shown):
  - JEZERO_SUMMARIZER_API_KEY: missing
```

The summarizer and its API key are missing on purpose. The
[summarizer](../glossary.md#summarizer) is the language model that turns a raw session into
an item, and the sample items you import next are already summarised.

## 3. Create the library

```bash
uv run jezero init
```

You should see:

```text
database ready at /home/me/jezero/data/jezero.db
```

The whole [library](../glossary.md#library) is this one SQLite file. Running `init` again
is safe: it only checks the file.

## 4. Import the sample items

The repository holds three synthetic items in `examples/items.example.jsonl`, one per line.
Look at the first one:

```bash
head -n 1 examples/items.example.jsonl | python3 -m json.tool
```

You should see:

```json
{
    "external_id": "demo-001",
    "occurred_at": "2026-09-02T10:15:00+00:00",
    "project": "billing-api",
    "title": "Database migration rollback plan",
    "summary": "The agent prepared a rollback plan for the invoices table migration and tested it on a copy of the staging database.",
    "questions": [
        "How do we roll back the invoices migration?",
        "Was the migration rollback tested?"
    ],
    "atoms": [
        {
            "kind": "decision",
            "text": "Keep the old column for one release before dropping it."
        },
        ...
    ]
}
```

This is an [item](../glossary.md#item): one session of an agent, written down as a title,
a short summary, the questions it answers, a project and the date it happened. Its
[atoms](../glossary.md#atom) are the small exact pieces worth reusing: here a decision, a
command that worked and the rationale. Search reads all of them.

Import the three items under the source name `demo`:

```bash
uv run jezero import-jsonl examples/items.example.jsonl --source demo
```

You should see:

```text
{"inserted": 3}
```

Run the same command again and Jezero reports `{"unchanged": 3}`: an item is identified by
its source and `external_id`, so importing twice never duplicates it.

## 5. Search from the terminal

```bash
uv run jezero search "migration rollback"
```

You should see:

```text
[1]   1.672  2026-09-02  demo  billing-api  Database migration rollback plan
```

Each line shows the item id, the score, the date, the [source](../glossary.md#source), the
project and the title. Your score differs a little: recent items get a boost, so the score
depends on today's date.

Matching ignores accents and works in any language, but it is literal. Try a Spanish word
from the third item, then an English word that appears nowhere:

```bash
uv run jezero search "despliegue"
uv run jezero search "deploy"
```

You should see:

```text
[3]   0.706  2026-10-18  demo  docs-site  Configuración del despliegue estático
no results
```

Jezero does not guess that *deploy* and *despliegue* mean the same thing. Agents are told
to try a synonym when a search comes back empty. [How search ranks
results](../explanation/search.md) explains why.

`jezero search` is the admin's view: it reads the database directly and sees every
source. Agents never use it.

## 6. Count what is stored

```bash
uv run jezero stats
```

You should see:

```json
{
  "items": 3,
  "sources": {
    "demo": {
      "items": 3,
      "latest": "2026-10-18T08:05:00+00:00"
    }
  }
}
```

## 7. Give the library a key

Agents read Jezero over the network, so the server asks each [client](../glossary.md#client)
for a [key](../glossary.md#key). The command calls it a token:

```bash
uv run jezero new-token
```

You should see:

```text
Give this token to the client (shown once, never stored):
  ***
Put this digest in the config:
  token_sha256 = "DIGEST"
```

Copy the token somewhere safe: Jezero prints it once and keeps only its SHA-256 digest.

Open `jezero.toml`, set `require_auth = true`, and add a client with the digest. The file
becomes:

```toml
# A local library for the tutorial. jezero.example.toml documents every field.
[storage]
path = "data/jezero.db"

[server]
host = "127.0.0.1"
port = 8765
require_auth = true

[[clients]]
name = "tutorial"
token_sha256 = "DIGEST"
```

A client with no `sources` line may read every source. Check the file again:

```bash
uv run jezero check-config
```

You should see, among the other lines:

```text
auth: on
...
clients:
  - tutorial: sources=['*']
```

## 8. Serve the library over MCP

Open a second terminal, go to the same `jezero` folder and start the server:

```bash
uv run jezero serve
```

You should see:

```text
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
...
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
```

Leave it running. The server is read-only: it has no tool that writes to the library.

## 9. Query it like an agent

Back in the first terminal, put the token from step 7 in a variable:

```bash
export JEZERO_TOKEN='TOKEN'
```

Then call the `search` tool the way an agent does, with one JSON-RPC request:

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $JEZERO_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "migration rollback"}}}' \
  | python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["result"]["structuredContent"], indent=2))'
```

You should see:

```json
{
  "count": 1,
  "results": [
    {
      "id": 1,
      "source": "demo",
      "external_id": "demo-001",
      "occurred_at": "2026-09-02T10:15:00+00:00",
      "title": "Database migration rollback plan",
      "summary": "The agent prepared a rollback plan for the invoices table migration and tested it on a copy of the staging database.",
      ...
      "project": "billing-api",
      "account": null,
      "original_ref": null,
      "trust": "own",
      "score": 1.6717
    }
  ]
}
```

Without the `Authorization` header the server answers `401 Unauthorized`. The other tools
(`get_item`, `later`, `timeline`, `list_sources`) are listed in [MCP
tools](../reference/mcp-tools.md).

## 10. Connect this computer

The Jezero app and the `jezero find` command read the library the way an agent does: with
a key that belongs to this computer, a [device](../glossary.md#device). Create one, using
`work-mac` as this computer's name:

```bash
uv run jezero connect --url http://127.0.0.1:8765 --device work-mac --read-only
```

You should see, below the Jezero name in block letters:

```text
Connect this computer to Jezero

Device key created in /home/me/.jezero (the key itself never leaves this computer).
Read-only: this computer's sessions are not uploaded.
Ask the Jezero admin to approve it with:

  jezero device approve work-mac SHA256 --read-only
```

`--read-only` means this computer searches Jezero but never uploads its own sessions. The
key stays in `~/.jezero`; the printed `SHA256` is its fingerprint, not a secret.

> [!NOTE]
> If this computer is already connected to a Jezero server, `connect` says so and stops,
> and the app keeps reading that server. Skip the rest of this step, and in step 11 open
> only the demo.

You are also the admin, so approve the key. Copy the line `connect` printed and add
`--sources "*"`:

```bash
uv run jezero device approve work-mac SHA256 --read-only --sources "*"
```

You should see:

```text
approved work-mac; restart the MCP server so it accepts the new key
```

A device may read `agent-*` sources by default, and the sample items live in `demo`;
`--sources "*"` lets this one read everything. The command added a `[[clients]]` block for
`work-mac` to `jezero.toml`.

The server reads its clients when it starts. In the second terminal, stop it with
`Ctrl+C` and start it again:

```bash
uv run jezero serve
```

Back in the first terminal, search with the new key:

```bash
uv run jezero find rollback
```

You should see:

```text
#1  Database migration rollback plan
      ▪ billing-api · 49 d ago

1 results · jezero show ID for the whole memory
```

The age (`49 d ago`) depends on today's date. The app calls items *memories*. Show the
whole item, atoms included:

```bash
uv run jezero show 1
```

You should see a panel with the title, the summary, a section **WHAT IT HOLDS** with the
three atoms and a section **ANSWERS QUESTIONS LIKE** with the questions.

## 11. Open the app

First try the app on made-up data, which needs no server:

```bash
uv run jezero ui --demo
```

You should see the home screen, with counters, fourteen days of activity, projects,
sources and the latest memories:

<p align="center">
  <img src="../app/home.svg" width="100%"
       alt="The Jezero app home with made-up data: counters, a 14-day activity chart, projects, sources and the latest memories.">
</p>

Press `q` to quit. Now open it on your own library:

```bash
uv run jezero ui
```

The top bar shows `work-mac`, `read-only` and `127.0.0.1:8765`, and the home screen lists
your three items under **Latest memories**. Press `2` for Search, type `rollback` and
press `Enter`: the result opens on the right with its atoms. Press `q` to quit. [Use the
Jezero app](../how-to/app.md) covers every view.

## 12. Connect a real agent (optional)

Any MCP client can now use the library with the key from step 7. With
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed, register the
server from the `jezero` folder:

```bash
claude mcp add --transport http jezero http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $JEZERO_TOKEN"
claude mcp list
```

You should see, among your other servers:

```text
jezero: http://127.0.0.1:8765/mcp (HTTP) - ✔ Connected
```

Start `claude` in that folder and ask *"Search Jezero: how do we roll back the invoices
migration?"*. The agent calls `search` and answers from item 1. Without a scope flag the
server is registered for this folder only; remove it with `claude mcp remove jezero`.

For day-to-day use, connect agents with the plugins instead: they bring the MCP server,
the habit of searching Jezero and the upload of every session. See [Connect an
agent](../how-to/connect-agents.md).

## What you built

- A library in `data/jezero.db` with three items, searchable from the terminal.
- An MCP server on `127.0.0.1:8765` that asks every client for its key.
- Two clients in `jezero.toml`: `tutorial`, a key you made with `new-token`, and
  `work-mac`, this computer's own key in `~/.jezero`.

Stop the server with `Ctrl+C` when you are done. Everything the tutorial created lives in
the `jezero` folder (`jezero.toml`, `data/`) and in `~/.jezero`.

## Next steps

- [Connect a second computer](connect-a-computer.md): give a laptop its own key and watch
  its first real session arrive in the library.
- [Add and tune sources](../how-to/sources.md): read your agents' real sessions, with a
  summarizer.
- [Run Jezero as a service](../how-to/deploy.md): keep the server and the connectors
  running, and reach them over HTTPS.
- [Configuration](../reference/configuration.md): every field of `jezero.toml`.
