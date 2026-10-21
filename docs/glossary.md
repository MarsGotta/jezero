# Glossary

The words the documentation uses, each with one meaning.

## Agent

A program that works for you with a language model: Claude Code, Codex, OpenCode, Hermes
Agent, Antigravity. Agents produce sessions and read Jezero.

## App

The terminal app for people that `jezero` opens. See [Use the Jezero app](how-to/app.md).

## Atom

One fact worth keeping, extracted from a session: a decision, its rationale, a fix, an
exact error, a command that worked, a fact or a risk. An item holds several. See
[Data model](reference/data-model.md#atoms).

## Client

Anything allowed to read the library: one `[[clients]]` block in `jezero.toml`, with one
key. A device is a client created by the plugins. See
[Configuration](reference/configuration.md#clients).

## Client core

`jezero_client.py`, one Python file with no dependencies that every plugin carries. It
creates the device's key, runs the hooks, queues uploads and talks to the server. See
[Client reference](reference/client.md).

## Connector

The part of the server that reads one source (an agent's session files, a task board)
and turns new material into documents to summarise. Each keeps a cursor so it only reads
what is new. See [Connectors](reference/connectors.md).

## Device

A computer connected with the plugins. It has its own key, made on that computer; the
server only stores the key's SHA-256 fingerprint. A device either uploads its sessions or
only reads. See [Manage computers and keys](how-to/devices.md).

## Hook

A command an agent runs at a moment of its life (session start, first message, end).
Jezero's hooks add context at the start and upload the session at the end. They never
block or break the agent.

## Inbox

The folder on the server where devices upload raw session files. Connectors read it
like any local store. See [HTTP API](reference/http-api.md).

## Item

One entry of the library: a session, or a part of a long one, a task or a thread,
summarised. It has a title, a summary, atoms, the questions it answers, a project, a source
and a date. The app calls items *memories*. See [Data model](reference/data-model.md).

## Key

The secret a client presents to the server, as `Authorization: Bearer ***`. Plugins
keep it in the device's client home and never print it.

## Library

Everything Jezero has stored: one SQLite file with full-text search.

## MCP

The [Model Context Protocol](https://modelcontextprotocol.io), the standard way agents
call tools. Jezero's MCP server is read-only. See [MCP tools](reference/mcp-tools.md).

## Plugin

The package that connects one agent to Jezero: the MCP server, the hooks and the skill.
See [Connect an agent](how-to/connect-agents.md).

## Project

What an item is about (`atlas`, `billing`). Jezero infers it from the session's folder
or the summary, and you can map folders to projects by hand. See
[Add and tune sources](how-to/sources.md).

## Protocol

The short text the session-start hook gives every agent: search Jezero by habit, check
with `later` before relying on something, cite item ids.

## Read-only computer

A device whose key may search but not upload, for computers whose sessions already reach
Jezero another way, or should not.

## Source

Where an item comes from: the name of its connector, such as `agent-codex-laptop`. Keys
are limited to the sources they may read.

## Summarizer

The language model that writes each item from the original text. A small, fast model is
enough. See [How summaries are made](explanation/summaries.md).

## Trust

`own` for your agents' work, `external` for text written by others. Agents see it on
every result.
