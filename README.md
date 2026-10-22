# Jezero

**A shared, searchable library of your agents' past work.**

[![ci](https://github.com/MarsGotta/jezero/actions/workflows/ci.yml/badge.svg)](https://github.com/MarsGotta/jezero/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-read--only-6a3ec0.svg)](https://modelcontextprotocol.io)

Your agents forget. Their sessions pile up as gigabytes of transcripts on several
machines, and the decisions in them are written down nowhere else. Jezero collects those
sessions, turns each one into a short summary plus **atoms** (decisions, rationale, fixes,
exact errors, commands that worked, facts, risks), and serves it all, read-only, to every
agent you run over [MCP](https://modelcontextprotocol.io).

<p align="center">
  <img src="docs/architecture.svg" width="100%"
       alt="Sessions from agents on the server and from other computers go through connectors, redaction and a summariser into one SQLite library, which agents query over a read-only MCP server.">
</p>

**[Documentation](https://jezero.marsgotta.com)** ·
[Tutorial](docs/getting-started/tutorial.md) ·
[Connect an agent](docs/how-to/connect-agents.md) ·
[Reference](docs/reference/cli.md) ·
[Changelog](CHANGELOG.md)

## What it looks like

You ask an agent about something another agent did weeks ago, on another computer. You
do not mention Jezero; the plugin taught it to look:

```text
you    > What did we end up doing with analytics on the shop site?

agent  · search("analytics shop site")
           [412] 2026-09-03  Shop site: remove the analytics script
           [431] 2026-09-10  Shop site: analytics back, only after consent
agent  · later(412, "analytics")
           [431] newer, same subject

agent  > It changed once. On 3 Sep the script was removed [412]; on 10 Sep it came
         back, loaded only after the visitor accepts the consent banner [431].
         That is the current state.
```

Each item is a summary with atoms, the date it happened and its project:

```text
[431] Shop site: analytics back, only after consent        2026-09-10 · shop
summary   The analytics script returns, loaded only after consent. No cookies before.
atoms     decision  Load analytics only after the visitor accepts the banner.
          why       Consent is required before any tracking.
          fix       The banner test ran before the script loaded; wait for the event.
          command   npm run test:consent
```

(Synthetic example. Jezero never ships real transcripts.)

## Features

- **Every agent, every computer.** Connectors for Claude Code, Codex, OpenCode, Hermes
  Agent, Antigravity and Paperclip tasks; plugins upload sessions from other computers.
- **Used by habit.** The plugins teach agents to search Jezero before working, to check
  whether a decision changed later, and to cite what they found, without being asked.
- **Write once, read many.** A small model summarises each session once; searches are
  plain full-text queries that take milliseconds and no GPU.
- **One SQLite file.** No database server to run; a backup is one file
  ([how](docs/how-to/deploy.md)).
- **Read-only by design.** Agents can search but never write; each key sees only the
  sources it is allowed to.
- **Private by default.** Secrets are redacted before anything is stored, keys are made on
  each computer, and the config never holds a secret.
- **An app for people.** A terminal app to search, watch activity and keep every agent's
  plugin up to date.

**Status: pre-alpha.** Everything above is implemented and tested; email comes next.

## Get started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/MarsGotta/jezero.git && cd jezero
uv sync
cp jezero.example.toml jezero.toml
uv run jezero init
uv run jezero import-jsonl examples/items.example.jsonl --source demo
uv run jezero search "migration rollback"
```

The [tutorial](docs/getting-started/tutorial.md) takes it from there: serve the library
over MCP and connect an agent, in about fifteen minutes.

## Connect your agents

Each computer makes its own key and the server approves it once
([how](docs/getting-started/connect-a-computer.md)). Then the plugins install straight
from this repository:

```bash
claude plugin marketplace add MarsGotta/jezero && claude plugin install jezero@jezero
codex plugin marketplace add MarsGotta/jezero    # then install Jezero from /plugins
hermes plugins install MarsGotta/jezero/plugins/hermes --enable
agy plugin install https://github.com/MarsGotta/jezero/tree/main/plugins/antigravity
```

OpenCode and any other MCP client: see [Connect an agent](docs/how-to/connect-agents.md).

## The Jezero app

`jezero` with no command opens a terminal app that searches the library the way an agent
does, shows what each source has been doing, shows the exact context an agent receives,
and installs or updates the plugins on this computer.

<p align="center">
  <img src="docs/app/home.svg" width="100%"
       alt="The Jezero app home: the name in block letters, counters, a 14-day activity chart, projects, sources and the latest items.">
</p>

```bash
uv tool install git+https://github.com/MarsGotta/jezero
jezero ui --demo        # look around with made-up data
```

More in [Use the Jezero app](docs/how-to/app.md).

## Documentation

Read it at **[jezero.marsgotta.com](https://jezero.marsgotta.com)**, or right here on
GitHub in [`docs/`](docs/README.md):

| | |
|---|---|
| **Learn** | [Your first library](docs/getting-started/tutorial.md) · [Connect a second computer](docs/getting-started/connect-a-computer.md) |
| **Do** | [Run as a service](docs/how-to/deploy.md) · [Sources](docs/how-to/sources.md) · [Monitor](docs/how-to/monitor.md) · [Agents](docs/how-to/connect-agents.md) · [Computers and keys](docs/how-to/devices.md) · [App](docs/how-to/app.md) · [Troubleshooting](docs/how-to/troubleshooting.md) |
| **Look up** | [Commands](docs/reference/cli.md) · [Configuration](docs/reference/configuration.md) · [Connectors](docs/reference/connectors.md) · [MCP tools](docs/reference/mcp-tools.md) · [HTTP API](docs/reference/http-api.md) · [Client](docs/reference/client.md) · [Data model](docs/reference/data-model.md) · [Glossary](docs/glossary.md) |
| **Understand** | [Architecture](docs/explanation/architecture.md) · [Search](docs/explanation/search.md) · [Summaries](docs/explanation/summaries.md) · [Security](docs/explanation/security.md) · [Design decisions](docs/explanation/design-decisions.md) |

## Contributing

Bug reports, ideas and pull requests are welcome: start with
[CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## Roadmap

1. Gmail connector (read-only scope, per thread, newsletters skipped).
2. A folder watcher for agents without hooks (Cursor) on other computers.
3. A retrieval benchmark command (questions with expected hits, top-k accuracy).
4. Optional vector search with sqlite-vec.

## The name

Jezero is the Martian crater that once held a lake. Its layered sediments keep the record
of what happened there, and the Perseverance rover sets samples aside in it so they can be
picked up later. That is the whole idea of this project.

## License

[Apache-2.0](LICENSE)
