# HTTP API

Every HTTP route of the Jezero server other than the MCP tools: device uploads, the key
check, the plugin bundle, the dashboard counts and the OAuth metadata. Each is served by
`jezero serve` on the same host and port as MCP.

## Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/mcp` | any client key | The MCP tools: see [MCP tools](mcp-tools.md). The path is `[server].path`. |
| `GET` | [`/inbox/v1/ping`](#check-a-device-key) | upload key | Check a device key. |
| `PUT` | [`/inbox/v1/{agent}/{relpath}`](#upload-a-session-file) | upload key | Upload one session file. |
| `GET` | [`/api/v1/overview`](#overview) | any client key | Counts per source, day and project. |
| `GET` | [`/client/jezero-client.tar.gz`](#client-bundle) | none | The plugin bundle. |
| `GET` | [`/.well-known/oauth-protected-resource/mcp`](#oauth-protected-resource-metadata) | none | Resource metadata, only with `require_auth`. |

- **Any client key**: `Authorization: Bearer <key>` of any `[[clients]]` entry.
- **Upload key**: the key of a client with `upload = true`, a device created by
  [`jezero device approve`](cli.md#jezero-device-approve).
- `GET` routes also answer `HEAD`. Another method gets HTTP 405 `Method Not Allowed`
  (plain text); an unknown path gets HTTP 404 `Not Found`.
- Errors of the inbox and overview routes are JSON: `{"ok": false, "error": "MESSAGE"}`.
  The message is safe to show to the user.

The inbox routes always need a key, even with `[server].require_auth = false`.

Every route, MCP included, checks the `Host` and `Origin` headers first, with the rules of
[the MCP endpoint](mcp-tools.md#authentication): an unknown host gets HTTP 421 with the
plain-text body `Invalid Host header`, an unknown browser origin HTTP 403 `Invalid Origin
header`, before the key is looked at. A computer that reaches the server by a name or
address missing from `public_url` and `allowed_hosts` is refused on every route.

Keys are read again from `jezero.toml` within a second of a change, so a key approved,
rotated or revoked with [`jezero device`](cli.md#jezero-device) needs no restart.

## Check a device key

```text
GET /inbox/v1/ping
```

Tells a device whether its key is approved for uploads. `jezero_client.py status` calls it.

```bash
curl -s https://YOUR-JEZERO/inbox/v1/ping -H "Authorization: Bearer $JEZERO_DEVICE_KEY"
```

You should see:

```json
{"ok": true, "device": "laptop", "version": "0.1.0.dev0"}
```

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true, "device": NAME, "version": VERSION}` | The key belongs to a client with `upload = true`. `device` is the client's name, `version` the server's. |
| 401 | `{"ok": false, "error": "missing bearer token"}` | No `Authorization` header, or not `Bearer`. |
| 401 | `{"ok": false, "error": "unknown token"}` | The key matches no client: not approved yet, revoked, or replaced by `jezero device rotate`. |
| 403 | `{"ok": false, "error": "this key may not upload"}` | The client exists but has `upload = false` (read-only). |
| 421 | `Invalid Host header` (plain text) | The `Host` header is not an allowed host. |

## Upload a session file

```text
PUT /inbox/v1/{agent}/{relpath}
```

Stores one raw session file in the device's inbox folder. The connectors read it later,
like a local store (see [Connectors](connectors.md#source-names)).

| Header | Required | Meaning |
|---|---|---|
| `Authorization` | yes | `Bearer <key>` of a client with `upload = true`. |
| `Content-Encoding` | no | `gzip`, `zstd`, or absent / `identity` for the raw bytes (case does not matter). Nothing else. |
| `X-Jezero-Project` | no | The project the device found for this session. Must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. |
| `Content-Length` | no | When larger than the limit, the upload is refused before the body is read. A body sent without it (chunked) is read up to the limit and refused with 413 as soon as it passes. |

The body is the file itself. `{agent}` and `{relpath}` must match one of these:

| `agent` | `relpath` pattern | Agent's file |
|---|---|---|
| `claude-code` | `projects/<folder>/<name>.jsonl` | A Claude Code transcript. |
| `codex` | `sessions/YYYY/MM/DD/rollout-<name>.jsonl` or `.jsonl.zst` | A Codex rollout, compressed or not. |
| `opencode` | `sessions/<name>.json` | An OpenCode session export. |
| `hermes` | `sessions/<name>.json` | A Hermes session export. |
| `antigravity` | `brain/<conversation>/transcript.jsonl` | An Antigravity transcript. |

`<folder>` and `<name>` are 1 to 200 characters of `A-Z a-z 0-9 . _ -` (Codex names
also allow `:`); `<conversation>` is 8 to 64 characters of `A-Z a-z 0-9 -`. A `..` segment
is refused.

The file is written to `<inbox.dir>/<device>/<agent>/<relpath>`, where `<device>` is the
client's name. The write is atomic: a newer upload of the same session replaces the file.
Its modification time is the upload time, so a device that was offline does not upload
behind the connector's cursor. With `X-Jezero-Project`, the value is written to
`<file>.project`, which the connectors use before any other project rule; without it, an
existing `<file>.project` is removed.

The size limit is `[inbox].max_mb` (default 64) × 1024 × 1024 bytes, measured after
decompression.

```bash
gzip -c ~/.claude/projects/-home-me-projects-billing/4b1c2d9e.jsonl | \
  curl -s -X PUT \
    https://YOUR-JEZERO/inbox/v1/claude-code/projects/-home-me-projects-billing/4b1c2d9e.jsonl \
    -H "Authorization: Bearer $JEZERO_DEVICE_KEY" \
    -H "Content-Encoding: gzip" \
    -H "X-Jezero-Project: billing" \
    --data-binary @-
```

You should see:

```json
{"ok": true, "device": "laptop", "bytes": 48213}
```

`bytes` is the size of the stored file, after decompression.

| Status | Error | When |
|---|---|---|
| 200 | | Stored. |
| 400 | `unknown agent 'cursor'; known: ['antigravity', 'claude-code', 'codex', 'hermes', 'opencode']` | `{agent}` is not in the table. |
| 400 | `path not accepted for claude-code: 'other/x.jsonl'` | `{relpath}` does not match the agent's pattern, or has a `..` segment. |
| 400 | `path escapes the inbox` | The resolved path leaves the device's folder. |
| 400 | `body is not valid gzip` | `Content-Encoding: gzip` with a body that is not gzip (`zstd`: `body is not valid zstd`). |
| 400 | `empty body` | The file is empty. |
| 400 | `invalid project name` | `X-Jezero-Project` does not match its pattern. |
| 401 | `missing bearer token` | No `Authorization: Bearer` header. |
| 401 | `unknown token` | The key matches no client. |
| 403 | `this key may not upload` | The client has `upload = false`. |
| 403 | `bad device name` | The client's name is not a valid device name. |
| 413 | `file too large` | Declared, received or decompressed size above the limit. |
| 415 | `unsupported Content-Encoding 'br'; use gzip, zstd or none` | Any encoding other than `gzip`, `zstd` or `identity`. |
| 421 | `Invalid Host header` (plain text) | The `Host` header is not an allowed host: see [Routes](#routes). |

The checks run in this order: host, key, agent and path, declared size, received size,
body decoding, empty body, project.

> [!NOTE]
> The client core keeps the whole queue after a 401 or 403 (the key may be approved for
> uploads later), a 5xx or a network error. After a 400, 413 or 415 it records the file as
> *rejected*, drops it from the queue and does not send it again until the file changes;
> `jezero_client.py status` lists the rejected files. See [the upload
> queue](client.md#upload-queue).

## Overview

```text
GET /api/v1/overview?days=14
```

Counts for a dashboard, over the sources the key may read: the `jezero` app uses it. It is
kept out of MCP so agents do not get one more tool to choose from.

| Query parameter | Type | Default | Meaning |
|---|---|---|---|
| `days` | integer | `14` | Days of daily counts, clamped to 1-90. Not a number: 14. |

```bash
curl -s "https://YOUR-JEZERO/api/v1/overview?days=3" -H "Authorization: Bearer $JEZERO_TOKEN"
```

You should see:

```json
{
  "ok": true,
  "device": "reader",
  "upload": false,
  "version": "0.1.0.dev0",
  "items": 4,
  "days": ["2026-10-19", "2026-10-20", "2026-10-21"],
  "sources": {
    "agent-claude-code-laptop": {"items": 3, "latest": "2026-10-20T21:23:12+00:00", "daily": [1, 1, 0]},
    "agent-codex-work-mac": {"items": 1, "latest": "2026-10-16T21:23:12+00:00", "daily": [0, 0, 0]}
  },
  "projects": {
    "billing": {"items": 3, "latest": "2026-10-20T21:23:12+00:00"},
    "atlas": {"items": 1, "latest": "2026-10-16T21:23:12+00:00"}
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `ok` | boolean | `true`. |
| `device` | string | The client's name; `"local"` when `require_auth = false`. |
| `upload` | boolean | Whether the key may upload. |
| `version` | string | The server's version. |
| `items` | integer | Items in the sources the key may read. |
| `days` | list of string | The last `days` UTC dates, oldest first, ending today. |
| `sources` | object | Per source: `items`, `latest` (`occurred_at` of the newest item) and `daily` (items per day, aligned with `days`, by `occurred_at`). |
| `projects` | object | Per project (items with no project are left out): `items` and `latest`, newest project first. |

When the key's `sources` match no stored source, the answer has the same shape with
nothing in it: `"items": 0`, the usual `days`, and empty `sources` and `projects`.

| Status | Body | When |
|---|---|---|
| 200 | as above | |
| 401 | `{"ok": false, "error": "unknown token"}` | No key, not `Bearer`, or a key that matches no client. Not with `require_auth = false`. |
| 421 | `Invalid Host header` (plain text) | The `Host` header is not an allowed host. |

## Client bundle

```text
GET /client/jezero-client.tar.gz
```

The plugin bundle that computers unpack before running `jezero_client.py init`: the client
core and every plugin. It is public code and needs no key. The file is
`[server].client_bundle` (default `dist/jezero-client.tar.gz`, relative to the config
file), built by `python3 scripts/build_clients.py`.

```bash
curl -fsSL https://YOUR-JEZERO/client/jezero-client.tar.gz | tar -xz -C ~/.jezero-src
```

| Status | Body | When |
|---|---|---|
| 200 | the file, `Content-Type: application/gzip` | |
| 404 | `{"ok": false, "error": "no client bundle built: run python3 scripts/build_clients.py on the server to build it"}` | The file does not exist. The server also logs a warning with the path at start. |
| 421 | `Invalid Host header` (plain text) | The `Host` header is not an allowed host. |

## OAuth protected resource metadata

```text
GET /.well-known/oauth-protected-resource/mcp
```

Served by the MCP library when `require_auth = true`, and named in the
`WWW-Authenticate` header of a 401 from `/mcp`. The path ends with `[server].path`.
Jezero runs no authorization server: keys are static and come from `[[clients]]`, so MCP
clients must be given the key as a header.

```json
{
  "resource": "https://YOUR-JEZERO/mcp",
  "authorization_servers": ["https://YOUR-JEZERO"],
  "bearer_methods_supported": ["header"]
}
```

`resource` is `[server].public_url`, or `http://HOST:PORT/PATH` from `[server]` when it is
not set.

## See also

- [MCP tools](mcp-tools.md): the tools behind `/mcp`.
- [Client](client.md): how the client core calls these routes.
- [Manage computers and keys](../how-to/devices.md): approving devices.
- [Deploy](../how-to/deploy.md): putting the server behind a reverse proxy.
