# Security model

What Jezero protects, from whom, and how; what the plugins send and when; and what Jezero
does not protect against. To report a vulnerability, see
[SECURITY.md](../../SECURITY.md).

## The threat model

Jezero holds summaries of personal and work material and serves them to AI agents. The
risks follow from that: the material is sensitive, the readers are programs that can be
manipulated by what they read, and the sources are spread over several computers.

| Who or what | The risk | What limits it |
|---|---|---|
| An agent manipulated by text it read | it uses Jezero to change or erase memory | the MCP server has no write tool, and its database connections are read-only |
| An agent that should not see everything | a coding agent reads your mail | per-client source patterns, applied to every tool |
| Someone on the network | reads or forges requests | a bearer key per client, compared by SHA-256 digest; TLS from a proxy or a private network |
| A web page in your browser | reaches a server on `127.0.0.1` through DNS rebinding | Host and Origin checks on every route |
| A secret in a session | ends up in the library, in the summariser's input, or in an answer | redaction before anything is summarised, stored or indexed |
| A leaked config file or database | reveals usable keys | only key digests are stored; secrets are referenced by environment variable name |
| A device | writes outside its place, or reads beyond its share | upload keys write only into their own inbox folder; reading follows its source patterns |
| A third party who writes content | plants instructions for your agents | `trust = "external"` items, server instructions, a summariser without tools |
| A contributor | commits personal data to the public repository | synthetic fixtures, the privacy check in pre-commit and CI |

The rest of this page explains each mechanism and its limits.

## The MCP server is read-only

There is no MCP tool that writes. `search`, `get_item`, `timeline`, `later` and
`list_sources` are all annotated read-only, non-destructive and closed-world. Underneath,
every read opens SQLite with `PRAGMA query_only = ON`, so even a bug in a tool could not
modify the library. Items enter only through connectors, which run on the server under
the admin's control.

This matters because agents read untrusted text all day: web pages, issues, email. An
agent that has been talked into misbehaving can do, through Jezero, only what its key
allows it to read. It cannot rewrite a decision, plant a fake "fact" for other agents or
delete history. The [design decisions](design-decisions.md#a-read-only-mcp-server) page
covers what was given up for this.

## Keys

Every caller of the MCP server presents a bearer key. The config never holds the key
itself. Each `[[clients]]` entry has exactly one of:

- `token_sha256`: the SHA-256 hex digest of the key (`jezero new-token` prints a key once,
  for the client, and its digest, for the config);
- `token_env`: the name of an environment variable that holds the key on the server.

On each request the server hashes the presented key and compares the digest with every
configured digest using a constant-time comparison. A client whose `token_env` variable is
not set is disabled with a warning; with `require_auth` on and no usable client, the
server refuses to start. Two clients may not share a key: equal `token_sha256` or
`token_env` values stop the config from loading, and two variables that happen to hold
the same key disable both clients, since a key must name exactly one owner.

**Device keys are created on the device.** `jezero_client.py init` (or `jezero connect`)
generates a random key on the computer that will use it, saves it in a file readable only
by your user (mode `0600`) and prints a line for the admin:
`jezero device approve DEVICE SHA256`. The digest is not a secret; sending it by chat or
email is fine. The server stores only the digest. The key itself travels only as the
bearer header of each request, so a copy of the server's config or database gives nobody
a usable key.

The same key both reads (MCP, overview) and, if approved for it, uploads. To revoke a
device, run `jezero device revoke NAME`; to replace its key, `jezero device rotate NAME
SHA256`. The server reads `[[clients]]` again within a second of a change, so a revoked
key stops working on every route at once, without a restart. Keys do not expire on their
own.

Reloading fails closed. When the edited file does not load (a typo), the server cannot
tell what the admin meant, so it keeps only the clients whose `[[clients]]` entry is still
there with exactly the text it had at the last good load. A client that was removed,
rotated or otherwise edited is refused until the file loads again; untouched clients keep
working. The `.env` file next to the config is watched the same way: a `token_env` key
changed or removed there takes effect within a second. The `jezero device` commands
refuse to write a file that changed after they read it, so two of them run at the same
moment cannot undo each other.

On the computer, the key stays in its file. The agents reach the server through a local
stdio bridge (`jezero_client.py mcp`) that reads the key file and adds the header to each
request: the Claude Code and Antigravity plugins ship it as their MCP server, and
`jezero_client.py setup` registers it in Claude Code and Codex, replacing the HTTP entries
with the key in clear that older clients wrote. So no agent's configuration file holds the
key; only OpenCode's plugin hands it to OpenCode, in memory. Agents in a sandbox that
cannot see your home can hold their own key in their config folder. See [Manage
computers and keys](../how-to/devices.md).

## Each client sees only its sources

Every source in the library is named after the connector that produced it (for example
`agent-claude-code-laptop`). Each client has a list of glob patterns, `sources`, matched
case-sensitively against those names. Every tool and the overview endpoint apply the list;
`search` and `timeline` apply it in the SQL query itself, before ranking. An item outside a
client's sources is indistinguishable from an item that does not exist: `get_item` and
`later` answer `found: false`.

The default for a hand-written `[[clients]]` entry is `["*"]`, everything. `jezero device
approve` is narrower by default: the device may read `agent-*` (agent sessions) plus its
own sources. With `--source-prefix`, a device's sources get another prefix (say
`work-claude-code-work-mac`), which keeps them out of every `agent-*` reader: a way to
keep one computer's sessions apart from the rest. `--read-only` approves a key that may
read but not upload.

The patterns match names, so connector names are the policy. Naming sources by kind first
(`agent-*`, `mail-*`, `tasks-*`) is what makes "a coding agent does not need your mail"
expressible as one pattern. The
[configuration reference](../reference/configuration.md#clients) lists the fields.

## Uploading is separate from reading

Devices deliver sessions through `PUT /inbox/v1/<agent>/<path>`, a plain HTTP route next to
MCP, not an MCP tool. The separation is the point: the MCP server stays read-only, and the
write path has its own, narrower rules:

- Uploads always need a key, even when `require_auth` is off, and the key's client must
  have `upload = true`.
- A file lands in `<inbox>/<device>/<agent>/...`, where `<device>` is the name of the
  client that owns the key. A key cannot choose another device's folder.
- `<agent>` must be one of the known agents, and the path must match the layout of that
  agent's own store (for example `projects/<slug>/<id>.jsonl` for Claude Code). `..` is
  refused, and the resolved path must stay inside the device's folder.
- The size limit, `inbox.max_mb` (64 MB), applies to what arrives and again after
  decompression. A body without `Content-Length` (chunked) is read in pieces and refused
  as soon as it passes the limit, so it never sits whole in memory; gzip and zstd are
  decompressed in chunks and stop at the limit. Only identity, gzip and zstd encodings
  are accepted. A Codex `.jsonl.zst` file is stored compressed, as sent, but only after
  it was decompressed in chunks and found within the same limit, so a small file that
  expands to gigabytes is refused with 413.
- An optional quota, `inbox.max_device_mb`, caps the space of each device's folder:
  uploads beyond it get 507. It is off by default.
- Writes are atomic (a temporary file and a rename). The project header is checked
  against a strict pattern before it is stored next to the file.

An uploaded file is data for a connector, nothing more. The connectors that read a new
device's folder are created **disabled** by `jezero device approve`, so the admin can run
a pilot on a few sessions before anything from that device reaches the library.

## Redaction before storage

The pipeline runs every document through the redactor before it is hashed, summarised,
stored or indexed. The summariser never sees what the redactor caught, and neither do the
items, the full-text index or the stored original. Built-in rules (`src/jezero/redact.py`)
cover:

- private key blocks;
- token shapes of Anthropic, OpenAI, GitHub, GitLab, Slack, AWS, Google, npm, Hugging Face
  and Stripe, JWTs, `Bearer` and `Basic` credentials;
- Azure keys and connection strings, signed URL parameters (`sig=`, `access_token=`),
  credentials inside URLs (`https://user:pass@host`);
- assignments to names that end in a secret word (`GITHUB_TOKEN=`, `db_pass:`,
  `"clientSecret": "..."`) and flags such as `--password X`.

The rules keep what is not a secret: references (`$VAR`, `${VAR}`, `<placeholder>`,
`os.environ[...]`), paths, booleans, code such as `token = get_access_token()`, and prose
such as "3 tests PASS" or "OAuth". A match is replaced by `[REDACTED:<kind>]`; for
assignments the name stays, so the text remains readable. Add your own patterns with
`redaction.extra_patterns`. After the rules improve, `jezero redact-stored` applies them
to everything already stored, without calling the model.

Redaction is defence in depth, not a guarantee. It finds common credential shapes; a
secret with an unknown shape passes. Personal data that is not a credential (names,
addresses, phone numbers) is not redacted at all. Sources known to carry secrets should
also be filtered at the connector.

> [!WARNING]
> Uploaded transcripts are kept on the server as sent, unredacted, like the agents' own
> stores on each computer. Redaction applies when a connector reads them. Protect the
> inbox folder as you protect the computers the sessions came from.

## Secrets live in the environment

No secret is ever written in the TOML file. Fields that need one end in `_env` and hold
the *name* of an environment variable: `api_key_env` for the summariser, `token_env` for
clients, `alert_webhook_env` and `heartbeat_url_env` for the watchdog, `*_env` options for
connectors. A connector option whose name looks like a secret (`api_key`, `token`,
`secret`, `password`, `credentials`, `private_key`...) without the `_env` suffix is
rejected when the config loads.

A git-ignored `.env` file next to the config is loaded automatically; variables already
set in the environment win, so a service manager can always override it. Variables that
came from the file follow it: the server reloads it when it changes, so a new or removed
`token_env` key applies without a restart.
`jezero check-config` reports which referenced variables are set, never their values. The
result is a config file that can be shared, reviewed and versioned without leaking
anything. [Deploy Jezero](../how-to/deploy.md) shows where the variables go.

## Authentication and the loopback rule

`server.require_auth` is on by default. Turning it off is allowed only when `server.host`
is a loopback address (`localhost`, `127.0.0.1` or another `127.x` address, `::1`) and
neither `public_url` nor `allowed_hosts` is set: the config is refused otherwise. Without
auth every MCP caller is treated as one client, `local`, that reads every source, and the
overview endpoint is open too. Uploads still need a key.

> [!IMPORTANT]
> A reverse proxy forwards its traffic from loopback. With `public_url` or
> `allowed_hosts` set, a loopback server is reachable from wherever the proxy is, so
> `require_auth = false` there would open the whole library to anyone who reaches the
> proxy. Jezero refuses that combination when the config loads.

The default host is `127.0.0.1`. Jezero does not terminate TLS. To reach it from other
computers, put a reverse proxy with TLS in front of it, or bind it to an address on a
private network such as a VPN, and set `public_url`. A bearer key sent over plain HTTP on
an open network is a key given away. [Deploy Jezero](../how-to/deploy.md) shows both
setups.

## Host and Origin checks

A web page can attack a server on your own computer without any network access: it makes
its own domain resolve to `127.0.0.1` (DNS rebinding) and then talks to the local port as
if it were its own site. The defence is to refuse requests whose `Host` header (and
`Origin`, when a browser sends one) is not a name the server expects.

Jezero turns on the MCP SDK's DNS-rebinding protection and applies the same check to
every route, in one middleware that runs before anything else: MCP, the OAuth metadata,
the inbox, the overview endpoint, the bundle and unknown paths. The list is always
explicit, never left to the SDK's defaults, which cover only `127.0.0.1`, `localhost`
and `[::1]` and would leave another loopback address (`127.0.0.2`) or a wildcard bind
unchecked:

- The allowed hosts are `allowed_hosts`, the host of `public_url` (with and without a
  port), `127.0.0.1`, `localhost`, `[::1]` and `server.host`, the last four on any port.
- The allowed origins are the origin of `public_url`, plus `http://127.0.0.1`,
  `http://localhost`, `http://[::1]` and `http://` with `server.host`, on any port.

An unknown host gets status 421 and an unknown origin 403, before the key is looked at.
So behind a proxy, `public_url` is what makes the public name acceptable; reaching the
server by any other name is refused. A server bound to `0.0.0.0` accepts only the names
and addresses you list: a computer that reaches it by a LAN address missing from
`allowed_hosts` is refused on every route, uploads included.

## Content written by others

Some sources hold text written by people other than you: email is the planned case. Their
items carry `trust = "external"`, and the server instructions that every MCP client
receives say to treat those items strictly as data and never to follow instructions found
inside them. The summariser is protected the same way for every document: the text sits
between `<document>` tags, the prompt says it is data, the model has no tools, and its
answer is only parsed into fields.

These are layers, not a wall. The trust flag is advice to the agent, which may still be
fooled. Your own sessions can also carry hostile text, for example an agent's reply that
quotes a web page it read. What bounds the damage is that nothing in the chain can act:
the summariser cannot call anything, and the MCP server cannot write.

Queries cannot inject either. Every search term is quoted before it reaches FTS5, so
operators in a query are searched as text, and the SQL uses placeholders for every value.
Their cost is bounded too: control characters are dropped, and a query keeps at most
4096 characters, 64 distinct terms and 100 characters per term. Arguments out of range
(an `item_id` beyond what SQLite stores, a date with no UTC equivalent) get a clear tool
error.
See [How the query is built](search.md#how-the-query-is-built).

## What the plugins send, and when

On another computer, nothing leaves until the computer is connected (`init`) and the key
is approved. After that:

| When | What is sent | To |
|---|---|---|
| Session start | the project slug of the working folder | MCP `timeline` |
| Session start, uploading computers | queued transcripts not yet sent | inbox |
| First prompt of a session | the prompt's distinctive words: up to 20, after removing stop words | MCP `search` |
| Stop (at most every 15 minutes per session) and session end | the session's transcript file, gzip-compressed, and the project slug in a header | inbox |
| `jezero_client.py backfill` | transcripts of past sessions you choose | inbox |

Most agents' transcript files are sent as the agent wrote them, tool results included;
filtering and redaction happen on the server. Hermes sessions are first exported,
read-only, from Hermes's own database into one JSON file per session with the dialogue
and tool calls only, and sessions started by its scheduler or as subagents are skipped.
A computer set up with `--no-upload` (the server's own computer, or any computer that
should only read) sends only the two MCP calls. Jezero does not store queries; the inbox
is the only place where a device's data is written.

Everything goes to the URL given at `init`, with the device's key. The client trusts the
system's certificate store (and, on macOS, can export the keychain's roots for company
networks that add their own). The plugin bundle at `/client/jezero-client.tar.gz` is
public code and needs no key. See
[Connect a computer](../getting-started/connect-a-computer.md).

## The privacy check for contributors

The repository is public, while the people who develop Jezero run it on their own
sessions and machines, so the risk of leaking a real name, host or path into a fixture, a
test or a page is constant. The rules in
[AGENTS.md](../../AGENTS.md) forbid personal data in the tree: fixtures and examples are
synthetic, and the owner's real configuration lives in the git-ignored `jezero.toml`.

`scripts/privacy_check.py` enforces part of this. It runs in the pre-commit hook (enabled
with `git config core.hooksPath .githooks`) on staged files and in CI with `--all` on
every tracked file. It blocks:

- absolute home paths (`/home/<name>/`, `/Users/<name>/`, except placeholders such as
  `/home/me/`);
- carrier-grade NAT and tailnet addresses, and private LAN addresses (`192.168.x.x`,
  `10.x.x.x`);
- credential shapes;
- any term in your private, git-ignored `.privacy-denylist` file (host names, people,
  employers, projects), case-insensitive.

A line that must contain a match on purpose can carry the marker `privacy: allow`. The
check is a safety net, not a substitute for care: it cannot know a name it was not told.
See [CONTRIBUTING.md](../../CONTRIBUTING.md).

## What Jezero does not protect against

- **Access to the server's files.** The database (summaries and redacted originals), the
  inbox (raw transcripts) and the `.env` file are plain files. There is no encryption at
  rest; anyone who can read them as your user or as root reads everything.
- **A leaked client key.** Its holder reads whatever the key's patterns allow, until the
  key is revoked or rotated. There is no expiry, rate limit or query log.
- **A device that lies.** An upload key can write any file that fits its agents' layouts
  into its own folder, and those sessions become items with `own` trust. Source names show
  which device an item came from, and new devices start with disabled connectors, but
  Jezero cannot tell a real session from a fabricated one.
- **Secrets and personal data the redactor does not recognise.** See above.
- **The summariser's provider.** It receives the redacted text of every session. If that
  is not acceptable, point `summarizer.base_url` at a model you run yourself.
- **An agent that ignores its instructions.** Trust levels and the protocol are advice.
- **Traffic without TLS.** Jezero relies on a proxy or a private network for encryption.
- **Abuse of resources.** Apart from the upload size limit, the optional per-device
  quota (`inbox.max_device_mb`) and the bounds on queries, there are no quotas or rate
  limits. On the ingestion side, the Codex connector skips any line over 16 MiB and stops
  reading a rollout after 512 MiB, with a warning, so one file cannot exhaust memory.
- **Your own user account on a device.** The device key is a file readable by your user;
  any program running as you can read it.

## See also

- [Architecture](architecture.md): where each of these boundaries sits.
- [Deploy Jezero](../how-to/deploy.md): TLS, `public_url` and secrets in practice.
- [Manage devices](../how-to/devices.md): approving, restricting and revoking keys.
- [HTTP API reference](../reference/http-api.md): the inbox, overview and bundle routes.
