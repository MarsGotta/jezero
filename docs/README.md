# Jezero documentation

Jezero gives your agents a shared memory of past work. It reads the sessions of every agent
you run, on every computer, writes each one down as a short summary with its decisions,
fixes, errors and commands, and lets any agent search it over a read-only MCP server.

New here? Start with the tutorial: in about fifteen minutes you have a library on your
computer, search it, and serve it to an agent.

**[Your first library →](getting-started/tutorial.md)**

## Learn

Step by step, from nothing to a working setup.

- [Your first library](getting-started/tutorial.md): install Jezero, fill a library with
  sample items, search it and serve it over MCP.
- [Connect a second computer](getting-started/connect-a-computer.md): give a laptop its
  own key, install a plugin and watch its first session arrive.

## Do

Guides for a goal you already have.

**Running the server**

- [Run Jezero as a service](how-to/deploy.md): systemd, HTTPS, backups and upgrades.
- [Add and tune sources](how-to/sources.md): connectors, projects, redaction, summaries.
- [Keep an eye on Jezero](how-to/monitor.md): health checks, alerts and logs.
- [Measure search quality](how-to/measure-search.md): a question file and a score to compare.

**Connecting agents and computers**

- [Connect an agent](how-to/connect-agents.md): Claude Code, Codex, OpenCode, Hermes Agent,
  Antigravity, or any MCP client.
- [Manage computers and keys](how-to/devices.md): add, list, limit, rotate and revoke keys.
- [Use the Jezero app](how-to/app.md): look, search and update plugins from a terminal.
- [Troubleshooting](how-to/troubleshooting.md): symptoms, causes and fixes.

## Look up

Exact and complete. The first two are generated from the code.

- [Command reference](reference/cli.md): every command of `jezero` and of the client core.
- [Configuration](reference/configuration.md): every key of `jezero.toml`.
- [Connectors](reference/connectors.md): every connector type and its options.
- [MCP tools](reference/mcp-tools.md): what agents can call, with arguments and results.
- [HTTP API](reference/http-api.md): uploads, the overview and the client bundle.
- [Client](reference/client.md): the client core's files, settings and hooks.
- [Data model](reference/data-model.md): items, atoms, sources and trust.
- [Glossary](glossary.md): the words these pages use.

## Understand

Why Jezero works the way it does.

- [Architecture](explanation/architecture.md): the parts, and the life of a session.
- [How search ranks results](explanation/search.md): literal matching, BM25 and recency.
- [How summaries are made](explanation/summaries.md): items, atoms and their cost.
- [Security and privacy](explanation/security.md): what is protected, and from whom.
- [Design decisions](explanation/design-decisions.md): what was chosen, and what was not.

## Contribute

- [Contributing](../CONTRIBUTING.md): set up, test and send a change.
- [Writing the docs](STYLE.md): how these pages are organised and written.
- [Changelog](../CHANGELOG.md) and [security policy](../SECURITY.md).
