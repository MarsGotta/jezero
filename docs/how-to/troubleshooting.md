# Troubleshooting

Find your symptom, then read why it happens and how to fix it. Most problems show up in
three places: `jezero_client.py status`, the client's log, and the server's log.

## First checks

Run these on the computer with the problem. The client core is in the bundle you
unpacked (`~/.jezero-src/jezero-client`, see [Connect an
agent](connect-agents.md#before-you-start)).

1. Check the key and the server:

   ```bash
   python3 ~/.jezero-src/jezero-client/jezero_client.py status
   ```

   You should see the client home in use (`key: /home/me/.jezero`), `HTTP 200`, the
   number of queued sessions and of rejected uploads, or `read-only, N readable sources`
   on a read-only computer. Add `--agent claude-code` (or `codex`, `antigravity`...) to
   check the key that agent uses.

2. Read the client's log, `client.log` in the [client
   home](devices.md#where-the-key-lives) (`~/.jezero/client.log` by default). Hooks and
   uploads write their errors there and nowhere else.

3. Run the session-start hook by hand, from a project folder:

   ```bash
   echo '{"session_id": "test", "cwd": "/home/me/projects/atlas"}' | python3 ~/.jezero-src/jezero-client/jezero_client.py hook session-start --agent claude-code
   ```

   You should see the protocol, followed by the project's recent work when Jezero has
   some:

   ```text
   ## Jezero: shared memory of past work (read-only)
   ...
   Recent work on project `atlas` in Jezero (newest first):
   - [212] 2026-09-10 Atlas: run the migration in batches
   ```

   Nothing at all means the hook found no key in this client home.

An agent that keeps its key in its own folder (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`JEZERO_CLIENT_HOME`) uses a different client home than your shell. Run these checks with
the same variables set, or with `--agent`.

On the server, two commands show the other side:

```bash
jezero device list
jezero connector list
```

The first lists every key with what it may read and whether it may upload; the second
every connector with its last run, last success, error and items.

## Keys and connection

### `HTTP 401 unknown token`

**What you see.** `status` prints
`server https://YOUR-JEZERO: HTTP 401 {"ok":false,"error":"unknown token"}`. On a
read-only computer: `cannot read https://YOUR-JEZERO: mcp list_sources: HTTP 401`. Agents
report the `jezero` MCP server as failed or asking for authentication.

**Why.** The server has no `[[clients]]` block with this key's fingerprint. Either the
key was never approved, it was revoked, the fingerprint was copied wrong, or the computer
made a new key with `init --force` afterwards. The key may also come from another client
home than the one you approved (the `key:` line of `status` says which).

**Fix.** On the server, `jezero device list` shows the approved names. Approving takes
effect within a second, with no restart. If the computer made a new key, give the server
its fingerprint with [`jezero device rotate`](devices.md#rotate-a-key). If `jezero.toml`
no longer loads, the server keeps the keys it had: see [A change to `jezero.toml` has no
effect](#a-change-to-jezerotoml-has-no-effect).

### `HTTP 403 this key may not upload`

**What you see.** `status` prints
`HTTP 403 {"ok":false,"error":"this key may not upload"}`, followed by `uploads refused
since ...` when sessions wait. See [Uploads refused](#uploads-refused).

**Why.** The computer uploads, but the server approved its key with `--read-only`
(`upload = false`).

**Fix.** Decide what the computer should be and make both sides agree: see [Read-only or
uploading](devices.md#read-only-or-uploading). On a computer that should only read, add
`"upload": false` to `client.json` and the warnings stop.

### `init` says the computer is already set up

**What you see.**
`already set up (/home/me/.jezero/client.json); use --force to make a new key`.

**Why.** The client home already holds a key. `init` never replaces one by accident.

**Fix.** Keep the existing key if it works (`status`). `--force` makes a new key, which
the admin must then give to the server with `jezero device rotate`: see [Rotate a
key](devices.md#rotate-a-key).

### Server not reachable

**What you see.** `status` prints `cannot reach https://YOUR-JEZERO: <urlopen error ...>`
(connection refused, name not found, timeout). Hooks give the protocol but no recent
work, and the Hermes tools and the MCP bridge answer `Jezero is not reachable right now`.

**Why.** The computer cannot reach the server: the server is down, the private network or
VPN is off, or the name does not resolve on this computer. Agents in a sandbox may have
no network access at all.

**Fix.** Check the server (`systemctl --user status jezero-serve.service`) and the
network, then `status` again. Nothing is lost meanwhile: uploads wait in the queue.

### `Invalid Host header`

**What you see.** Requests get status 421 with the body `Invalid Host header`: `status`
prints `HTTP 421 Invalid Host header`, uploads stay queued, and agents cannot reach the MCP
server.

**Why.** The server rejects requests for a host name it does not know (protection
against DNS rebinding), on every route: MCP, uploads, the key check, the dashboard and
the bundle. It knows `127.0.0.1`, `localhost`, `[::1]`, its own `server.host`, the host of
`server.public_url` and the names in `server.allowed_hosts`. A server listening on
`0.0.0.0` that a computer reaches by a LAN address (`http://192.0.2.10:8765`) gets 421
until that address is listed.

**Fix.** Set `public_url` to the address computers use, add any other name or address to
`allowed_hosts`, and restart the server: `[server]` settings are read at start. See [Reach
it from other computers](deploy.md#reach-it-from-other-computers).

### TLS or certificate errors

**What you see.** `status` or `client.log` shows `CERTIFICATE_VERIFY_FAILED`, typically on
macOS.

**Why.** Python from python.org on macOS ships without the system's certificates, and
company networks add their own root certificate to the keychain, which Python does not
read.

**Fix.** On macOS the client fixes it by itself: on the first certificate error it
exports the keychains' certificates to `ca.pem` in the client home and retries, then uses
that file from then on. If it still fails, or on another system, point the client at a
certificate bundle that holds your root certificate:

```bash
export JEZERO_CA_FILE=/home/me/certs/company-root.pem
```

Set it where the agent runs, not only in your shell. The fix covers the client core
(hooks, uploads, the Hermes tools and the MCP bridge that Claude Code, Codex and
Antigravity use). OpenCode connects to the MCP server itself and uses its own trust
settings, as does any MCP client you configured with a URL.

### The bundle download fails with 404

**What you see.** `curl` on `https://YOUR-JEZERO/client/jezero-client.tar.gz` gets 404,
with `{"ok":false,"error":"no client bundle built: run python3 scripts/build_clients.py in
a checkout, or download jezero-client.tar.gz from the GitHub release of this version"}`.
The server's log warned about it at start: `client bundle not found at ...`.

**Why.** The server offers the bundle only once the file named by `server.client_bundle`
exists: built in a checkout, or downloaded when Jezero was installed from PyPI.

**Fix.** On the server, build it (checkout) or download and verify the release's bundle
(PyPI): see [Serve the plugin bundle](deploy.md#serve-the-plugin-bundle).

## Server

### `check-config` lists problems

**What you see.** `jezero check-config` ends with a `problems:` list and exits with 1:

```text
problems:
  - summarizer.model is not set, and 2 connector(s) are enabled
  - JEZERO_SUMMARIZER_API_KEY (summarizer.api_key_env) is not set, and 2 connector(s) are enabled
  - client admin: JEZERO_ADMIN_TOKEN is not set, so it cannot log in
```

**Why.** The file is valid, but something it relies on is missing: enabled connectors
need a summarizer model and its API key, a client with `token_env` needs that variable,
and `require_auth` needs at least one client. A library without enabled connectors, such
as the one of [the tutorial](../getting-started/tutorial.md), needs no summarizer.

**Fix.** Set what each line names, in `jezero.toml` or in the `.env` file next to it (see
[Configuration](../reference/configuration.md)), and run `check-config` again until it
exits with 0. A file that does not load at all prints `invalid config:` and the reason
instead, and exits with 2.

### A connector fails with `folder not found`

**What you see.** `jezero run` prints, for example,
`{"connector": "agent-claude-code", "error": "FileNotFoundError: projects_dir folder not
found: /home/me/.claude/projects"}`, and `jezero health` reports it as `failing`.

**Why.** A folder or file named in the connector's options does not exist: a typo, an
agent not installed on this computer, or a disk not mounted. Every connector type checks
its own option (`projects_dir`, `sessions_dir`, `export_dir`, `transcripts_dir`,
`data_dir`, `state_db`, `db_path`). For a `sessions_dir` with a wildcard, the folder
before the first wildcard must exist: `/home/me/agents/*/codex/sessions` fails with
`sessions_dir folder not found: /home/me/agents`.

**Fix.** Correct the path, or set `enabled = false` until the source exists. Inbox
connectors written by `jezero device approve` get their folders created, so this error
there means the inbox moved.

### A change to `jezero.toml` has no effect

**What you see.** A key you approved does not work yet, or a key whose entry you edited
stops working, and the server's log has `config changed but could not be loaded (...);
keeping the previous clients whose [[clients]] entry is unchanged (...) and refusing the
keys of those removed or edited (...)`.

**Why.** The server reads `[[clients]]` again whenever the file (or the `.env` next to it)
changes, but only takes the new list if the whole file still loads. Otherwise it fails
closed: clients whose entry is exactly as it was keep working, so a typo does not lock
everyone out, while a removed or edited client is refused, so a revocation is never
undone by a typo. Other settings, such as `[server]` and `[inbox]`, are read only at
start.

**Fix.** Run `jezero check-config`, fix what it reports, and save the file again. The
`jezero device` commands check the file before they write it and keep a copy,
`jezero.toml.bak-<UTC time>`, next to it. If one says the file `changed since it was
read`, another command or an editor wrote it in the meantime: nothing was changed, run it
again. After changing `[server]` or `[inbox]`, restart
the server.

## Agents

### The agent never uses Jezero, or its tools are missing

**What you see.** The agent answers without searching, or has no `search` tool from
`jezero`.

**Why and fix, per agent:**

- **Claude Code**: the plugin brings the hooks, the skill and the MCP server (the stdio
  bridge). Check `claude plugin list` and `claude mcp list`; restart Claude Code after
  installing. Without the plugin, run `jezero_client.py setup --agent claude-code`. If
  `setup` says `claude: not found, skipped`, the `claude` command is not on `PATH` (nor
  in `~/.local/bin`). If the bridge answers `Jezero is not connected on this computer (no
  key in ...)`, the key is in another client home: see [Agents in a
  sandbox](devices.md#agents-in-a-sandbox).
- **Codex**: the plugin brings hooks and the skill, not the MCP server. Run
  `jezero_client.py setup --agent codex` and restart Codex. Check with `codex mcp list`.
  If `setup` says `codex: not found, skipped`, neither `CODEX_HOME` nor the `codex`
  command exists.
- **OpenCode**: the plugin registers the MCP server only when it finds a key as OpenCode
  starts. Restart OpenCode after `init`. An `mcp.jezero` entry of your own in OpenCode's
  config takes precedence, with whatever key it holds.
- **Hermes Agent**: the `jezero_*` tools appear only when the plugin finds a key. Check
  that `JEZERO_CLIENT_HOME`, if you use it, is set in the environment Hermes runs in (a
  service has its own), and restart Hermes.
- **Antigravity**: `agy plugin list` must show `Components: 1 skill, 1 MCP server, 1
  hook`. If it shows no MCP server, it was installed from the bare repository URL:
  reinstall with the full `/tree/main/plugins/antigravity` path (see [Connect an
  agent](connect-agents.md#antigravity)).

If the tools are there but the agent rarely calls them, check that the session-start
hook runs (next entry): the protocol is what makes searching a habit.

### The app says `old MCP entry with the key in clear: run setup`

**What you see.** In the app's Agents view, Claude Code or Codex shows `old MCP entry
with the key in clear: run setup`.

**Why.** An older client wrote the key into the agent's configuration (`~/.claude.json`,
or `url` and `http_headers` in Codex's `config.toml`).

**Fix.** Run `jezero_client.py setup --agent claude-code` (or `codex`). It removes the old
entry and, unless the Claude Code plugin already brings the bridge, registers the bridge
in its place. Then treat the key as exposed if those files were shared or backed up:
[rotate it](devices.md#rotate-a-key).

### Hooks do not run

**What you see.** No Jezero protocol or recent work at session start, and no uploads,
while `status` works.

**Why.** The agent does not run the plugin's hooks: the plugin is disabled, Codex has not
trusted them, or `python3` is not on the `PATH` the agent gives its hooks. Desktop apps
often start with a shorter `PATH` than your terminal.

**Fix.**

1. Check the plugin is enabled: `claude plugin list`, `codex plugin list`, `agy plugin
   list`, `hermes plugins list`.
2. For Codex, trust the hooks (next entry).
3. Run the hook by hand (see [First checks](#first-checks)). If it works in your shell,
   make `python3` available to the agent, for example by starting the agent from a
   terminal.

### Codex hooks do not run

**What you see.** Codex has the Jezero plugin, but sessions start without context and are
never uploaded.

**Why.** Codex runs a plugin's hooks only after you trust them.

**Fix.** Open Codex, run `/hooks` and trust the Jezero hooks. Do it again if an update
changes them.

### Headless `agy` denies the Jezero tools

**What you see.** In a headless Antigravity run, calls to the Jezero tools are denied.

**Why.** Antigravity asks for permission before the first call of each MCP tool, and a
headless run has nobody to ask.

**Fix.** Allow the Jezero server's tools in `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp(jezero_jezero/*)"]
  }
}
```

### `claude -p` denies the Jezero tools

**What you see.** A non-interactive `claude -p` run reports that it has no permission to
use the Jezero `search` tool (`mcp__plugin_jezero_jezero__search` with the plugin,
`mcp__jezero__search` with a server added by `setup` or by hand).

**Why.** Claude Code asks before using an MCP tool, and `-p` runs cannot ask.

**Fix.** Add `"mcp__plugin_jezero_jezero"` (plugin) or `"mcp__jezero"` (`setup`) to
`permissions.allow` in `~/.claude/settings.json` (or in `CLAUDE_CONFIG_DIR`), or pass the
same name to `--allowedTools`. See [Connect an agent](connect-agents.md#claude-code).

## Uploads

### Nothing gets uploaded

**What you see.** Sessions from this computer never appear in Jezero.

**Why and fix, in the order to check:**

1. **The computer is read-only.** `status` says `read-only`, and `backfill` says
   `claude-code: read-only computer (init --no-upload): nothing to upload`. See [Read-only
   or uploading](devices.md#read-only-or-uploading).
2. **The server refuses the key.** `status` shows `uploads refused since ...`: see
   [Uploads refused](#uploads-refused).
3. **The hooks do not run.** See [Hooks do not run](#hooks-do-not-run).
4. **The session is still going.** A stop uploads at most once every 15 minutes per
   session; the full session goes when it ends. OpenCode uploads when a session goes
   idle, Hermes Agent at the end of a turn. Run `jezero_client.py flush` to send what is
   queued now.
5. **The session is of a kind Jezero skips.** Claude Code subagent transcripts, and
   Hermes Agent scheduled jobs and subagent sessions, are never uploaded.
6. **The server rejected the file.** `status` shows `rejected uploads: N` and why: see
   [Rejected uploads](#rejected-uploads).
7. **It arrived but is not read yet.** Look in the device's inbox folder on the server
   (`data/inbox/NAME/AGENT/` by default). If the files are there, the connectors are the
   cause: `device approve` creates them disabled, and they wait until a session has been
   quiet for `settle_minutes` (30 by default). `jezero connector list` shows which are
   enabled. Pilot and enable them: see [Add a computer](devices.md#add-a-computer).
8. **Older sessions.** The plugins send sessions from the moment they are installed. Send
   earlier ones with [`backfill`](devices.md#the-upload-queue).

### Uploads stay queued

**What you see.** `status` keeps showing `queued sessions: N` with N above 0.

**Why.** The background flush stops at the first failure and keeps the rest, so an
unreachable server keeps everything queued. `client.log` says why, for example
`flush stopped at ...: <urlopen error [Errno 111] Connection refused>`. A server that
refuses the key keeps the queue too: see [Uploads refused](#uploads-refused). So does
HTTP 507 `device inbox full`: the device's folder reached the server's
[`inbox.max_device_mb`](../reference/configuration.md#inbox); raise it or clear old files
of that device in the inbox, then `flush`. Only one flush runs at a time; a flush that died
leaves a lock that expires after 10 minutes.

**Fix.** Fix the connection (see [Server not reachable](#server-not-reachable) and
[`Invalid Host header`](#invalid-host-header)), then send the queue:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py flush
```

You should see `"failed": 0` in its counts.

### Uploads refused

**What you see.** `status` shows a line like this, and the queue does not shrink:

```text
uploads refused since 2026-10-22 03:32: HTTP 403 {"ok":false,"error":"this key may not upload"} (the queue is kept)
```

`client.log` has, once: `uploads refused by the server (HTTP 403 ...): the queue stays
until the server's admin allows this key to upload (upload = true in its [[clients]]
block, picked up within a second)`.

**Why.** The server answered 401 (the key is unknown: not approved yet, revoked or
rotated) or 403 (the key may only read). The client keeps every queued session instead of
dropping them, since the key may be approved for uploads later.

**Fix.** On the server, check the device with `jezero device list`. For a 401, approve
the key, or rotate it if the computer made a new one; for a 403, allow uploads (see
[Read-only or uploading](devices.md#read-only-or-uploading)). The server picks the change
up within a second; then run `flush` on the computer. If the computer should only read,
add `"upload": false` to its `client.json` instead, and delete what waits in
`spool/pending/` of the client home.

### Rejected uploads

**What you see.** `status` shows `rejected uploads: N` and the newest ones:

```text
rejected uploads: 1
  2026-10-22 03:35 claude-code: HTTP 413 {"ok":false,"error":"file too large"}
```

**Why.** The server refused that file for good: HTTP 413 for a file above
[`inbox.max_mb`](../reference/configuration.md#inbox) (64 MB after decompression by
default, also for what a Codex `.jsonl.zst` file expands to), 400 for a path or body it
does not accept, 415 for an encoding it does not read. The client records the file as rejected and does not send it again until the file
changes. The other sessions are not held up.

**Fix.** For 413, raise `inbox.max_mb` on the server and restart it, then touch the
session file on the computer or let the agent change it, and `flush`. A 400 or 415 points
at a client older than the server: update the plugins (see [Connect an
agent](connect-agents.md)).

### Items have no project

**What you see.** Items from a computer have an empty or wrong project, and sessions
start without the project's recent work.

**Why.** The computer finds the project from the session's folder: your `paths` rules,
then the git repository, then the folder below a common root such as `projects` or
`code`. A session in a folder outside all three has no project, and the summariser
guesses one from the text.

**Fix.** Map the folder in `client.json` on that computer (see
[Projects](devices.md#projects)). The rules apply to sessions uploaded from then on. To
rename projects, or fix items already stored, use the server's
[`[projects]`](../reference/configuration.md#projects) aliases, then re-apply the hints:

```bash
jezero retag --connector agent-claude-code-laptop --dry-run
jezero retag --connector agent-claude-code-laptop
```

## Search

### Search finds nothing

**What you see.** `search` (or `jezero find`) returns no results for something you know
an agent did.

**Why.** Matching is literal: Jezero looks for the words themselves, as pieces of text,
in titles, summaries, questions and atoms. It ignores accents and case, but not synonyms
or other languages, and it drops words shorter than three characters. Other causes: the
key cannot read that source, or the session is not in the library yet.

**Fix.**

1. Search for 2 to 5 distinctive words: names, ids, a fragment of the error. Try a
   synonym, the singular, or the language summaries are written in
   ([`summarizer.output_language`](../reference/configuration.md#summarizer)).
2. Compare with the admin's view. On the server, `jezero search WORDS` sees every
   source; on the computer, `jezero find WORDS` sees what this key sees. If only the
   admin finds it, the key's `sources` do not cover that source: see [Limit what a key
   can read](devices.md#limit-what-a-key-can-read).
3. If neither finds it, the session has not been summarised yet: see [Nothing gets
   uploaded](#nothing-gets-uploaded).

See [How search ranks results](../explanation/search.md#how-words-match).

## The app

### The app says it is not connected

**What you see.** The app shows "Not connected yet" with one of these reasons:

- ``This computer is not connected to Jezero yet: run `jezero connect`.``: the app found
  no key. It looks in the same [client home](devices.md#where-the-key-lives) as the client
  core, so a key made with `JEZERO_CLIENT_HOME` needs that variable set for the app too.
- `Jezero is not reachable right now (...)`: see [Server not
  reachable](#server-not-reachable) and [TLS or certificate
  errors](#tls-or-certificate-errors).
- `The server answered HTTP 401: is this key still approved?`: see [`HTTP 401 unknown
  token`](#http-401-unknown-token).

**Fix.** Press `c` in the app, or run `jezero connect`, to make a key, then have it
approved. `jezero ui --demo` shows every view on made-up data while you wait.

## Next steps

- [Manage computers and keys](devices.md): keys, read-only computers and the upload
  queue.
- [Connect an agent](connect-agents.md): install, check and update each plugin.
- [Keep an eye on Jezero](monitor.md): the server's side, with health checks and logs.
