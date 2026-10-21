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

   You should see `HTTP 200` and the number of queued sessions, or `read-only, N readable
   sources` on a read-only computer.

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

An agent that keeps its key in its own folder (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`JEZERO_CLIENT_HOME`) uses a different client home than your shell. Run these checks with
the same variables set.

## Keys and connection

### `HTTP 401 unknown token`

**What you see.** `status` prints
`server https://YOUR-JEZERO: HTTP 401 {"ok":false,"error":"unknown token"}`. On a
read-only computer: `cannot read https://YOUR-JEZERO: mcp list_sources: HTTP 401`. Agents
report the `jezero` MCP server as failed or asking for authentication.

**Why.** The server has no `[[clients]]` block with this key's fingerprint. Either the
key was never approved, the server was not restarted after `jezero device approve`, the
fingerprint was copied wrong, or the computer made a new key with `init --force`
afterwards. The key may also come from another client home than the one you approved.

**Fix.** Check that `jezero.toml` has the device's block and restart the server. If the
key changed, approve the new fingerprint as in [Rotate a
key](devices.md#rotate-a-key). If you lost the approve line, run `init --force` and
approve the new one.

### `HTTP 403 this key may not upload`

**What you see.** `status` prints
`HTTP 403 {"ok":false,"error":"this key may not upload"}`, and `client.log` has lines
like `upload claude-code projects/...: HTTP 403`.

**Why.** The computer uploads, but the server approved its key with `--read-only`
(`upload = false`). The server refuses every upload, and the client drops the refused
sessions instead of retrying them.

**Fix.** Decide what the computer should be and make both sides agree: see [Read-only or
uploading](devices.md#read-only-or-uploading). On a computer that should only read, add
`"upload": false` to `client.json` and the warnings stop.

### `init` says the computer is already set up

**What you see.**
`already set up (/home/me/.jezero/client.json); use --force to make a new key`.

**Why.** The client home already holds a key. `init` never replaces one by accident.

**Fix.** Keep the existing key if it works (`status`). `--force` makes a new key, which
the admin must approve again.

### Server not reachable

**What you see.** `status` prints `cannot reach https://YOUR-JEZERO: <urlopen error ...>`
(connection refused, name not found, timeout). Hooks give the protocol but no recent
work, and the Hermes tools and the Antigravity bridge answer `Jezero is not reachable
right now`.

**Why.** The computer cannot reach the server: the server is down, the private network or
VPN is off, or the name does not resolve on this computer. Agents in a sandbox may have
no network access at all.

**Fix.** Check the server (`systemctl --user status jezero-serve.service`) and the
network, then `status` again. Nothing is lost meanwhile: uploads wait in the queue.

### `Invalid Host header`

**What you see.** Requests get status 421 with the body `Invalid Host header`.

**Why.** The server rejects requests for a host name it does not know (protection
against DNS rebinding). It knows `127.0.0.1`, `localhost`, the host of
`server.public_url` and the names in `server.allowed_hosts`.

**Fix.** Set `public_url` to the address computers use, add any other name to
`allowed_hosts`, and restart the server. See [Reach it from other
computers](deploy.md#reach-it-from-other-computers).

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
(hooks, uploads, the Hermes tools and the Antigravity bridge). Agents that connect to the
MCP server themselves, such as Claude Code, Codex and OpenCode, use their own trust
settings: for Claude Code, set `NODE_EXTRA_CA_CERTS` to the same file.

### The bundle download fails with 404

**What you see.** `curl` on `https://YOUR-JEZERO/client/jezero-client.tar.gz` gets 404,
with `{"ok":false,"error":"no client bundle built"}`.

**Why.** The server offers the bundle only after it has been built.

**Fix.** Build it on the server: see [Serve the plugin
bundle](deploy.md#serve-the-plugin-bundle).

## Agents

### The agent never uses Jezero, or its tools are missing

**What you see.** The agent answers without searching, or has no `search` tool from
`jezero`.

**Why and fix, per agent:**

- **Claude Code, Codex**: the plugin brings hooks and the skill, not the MCP server. Run
  `jezero_client.py setup --agent claude-code` (or `codex`) and restart the agent. Check
  with `claude mcp list` or `codex mcp list`. If `setup` says `claude: not found,
  skipped`, the `claude` command is not on `PATH` (nor in `~/.local/bin`); for Codex,
  neither `CODEX_HOME` nor the `codex` command exists.
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
use `mcp__jezero__search`.

**Why.** Claude Code asks before using an MCP tool, and `-p` runs cannot ask.

**Fix.** Add `"mcp__jezero"` to `permissions.allow` in `~/.claude/settings.json` (or in
`CLAUDE_CONFIG_DIR`), or pass `--allowedTools mcp__jezero` to the run. See [Connect an
agent](connect-agents.md#claude-code).

## Uploads

### Nothing gets uploaded

**What you see.** Sessions from this computer never appear in Jezero.

**Why and fix, in the order to check:**

1. **The computer is read-only.** `status` says `read-only`, and `backfill` says
   `read-only computer (init --no-upload): nothing to upload`. See [Read-only or
   uploading](devices.md#read-only-or-uploading).
2. **The hooks do not run.** See [Hooks do not run](#hooks-do-not-run).
3. **The session is still going.** A stop uploads at most once every 15 minutes per
   session; the full session goes when it ends. OpenCode uploads when a session goes
   idle, Hermes Agent at the end of a turn. Run `jezero_client.py flush` to send what is
   queued now.
4. **The session is of a kind Jezero skips.** Claude Code subagent transcripts, and
   Hermes Agent scheduled jobs and subagent sessions, are never uploaded.
5. **It arrived but is not read yet.** Look in the device's inbox folder on the server
   (`data/inbox/NAME/AGENT/` by default). If the files are there, the connectors are the
   cause: `device approve` creates them disabled, and they wait until a session has been
   quiet for `settle_minutes` (30 by default). Pilot and enable them: see [Add a
   computer](devices.md#add-a-computer), step 6.
6. **Older sessions.** The plugins send sessions from the moment they are installed. Send
   earlier ones with [`backfill`](devices.md#the-upload-queue).

### Uploads stay queued

**What you see.** `status` keeps showing `queued sessions: N` with N above 0.

**Why.** The background flush stops at the first failure and keeps the rest, so an
unreachable server keeps everything queued. `client.log` says why, for example
`flush stopped at ...: <urlopen error [Errno 111] Connection refused>`. Only one flush
runs at a time; a flush that died leaves a lock that expires after 10 minutes.

**Fix.** Fix the connection (see [Server not reachable](#server-not-reachable)), then send
the queue:

```bash
python3 ~/.jezero-src/jezero-client/jezero_client.py flush
```

You should see `"failed": 0` in its counts.

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
