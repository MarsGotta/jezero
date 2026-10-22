# Run Jezero as a service

Run Jezero on a Linux server so it collects sessions on a schedule, serves them to agents
on other computers over HTTPS, and survives reboots and upgrades. This guide covers the
install, the systemd units, the reverse proxy, the plugin bundle, backups and upgrades.

## Before you start

- A Linux server with systemd, [git](https://git-scm.com) and
  [uv](https://docs.astral.sh/uv/). uv brings Python 3.12 or newer; Jezero also needs
  SQLite 3.45 or newer, which recent Python builds include.
- An API key for the [summarizer](../explanation/summaries.md) model.
- Ideally, a first library made with [the tutorial](../getting-started/tutorial.md), so
  the commands here are familiar.

## Install Jezero

1. Clone the repository into `~/jezero` and install it. The systemd units expect this
   folder; if you choose another one, you edit two paths later.

   ```bash
   git clone https://github.com/MarsGotta/jezero.git ~/jezero
   cd ~/jezero
   uv sync
   ```

2. Check that the command works:

   ```bash
   .venv/bin/jezero --version
   ```

   You should see:

   ```text
   jezero 0.1.0.dev0
   ```

A checkout is the recommended way on the server: it holds the
[plugin bundle](#serve-the-plugin-bundle) sources and the systemd units.
`uv tool install git+https://github.com/MarsGotta/jezero` also gives you a `jezero`
command (in `~/.local/bin`), but without the checkout you cannot build the bundle, and you
must point the units at that command and at your config yourself.

## Configure it

1. Create the config from the example and adapt it: the summarizer, the
   [clients](../reference/configuration.md#clients) that may read, and the
   [sources](sources.md) to collect.

   ```bash
   cp jezero.example.toml jezero.toml
   ```

   Jezero looks for its config in this order: the path given with `-c` or `--config`,
   then the `JEZERO_CONFIG` environment variable, then `jezero.toml` in the current
   folder. The units run in `~/jezero`, so they find `~/jezero/jezero.toml`. Relative
   paths in the file, such as `storage.path = "data/jezero.db"`, are relative to the
   file's folder.

2. Put the secrets in a `.env` file next to the config. Jezero loads it on every command.
   The config only holds the *names* of the variables (keys ending in `_env`).

   ```bash
   touch .env
   chmod 600 .env
   ```

   Then add one `NAME=value` line per variable with an editor, for example
   `JEZERO_SUMMARIZER_API_KEY=YOUR-API-KEY`. Blank lines, `#` comments, an `export`
   prefix and quotes around the value are accepted. A variable already set in the
   environment wins over the file, so a service manager can always override it. `.env`
   is git-ignored.

3. Check the config and create the database:

   ```bash
   .venv/bin/jezero check-config
   .venv/bin/jezero init
   ```

   You should see:

   ```text
   config: /home/me/jezero/jezero.toml
   database: /home/me/jezero/data/jezero.db
   sqlite: 3.53.1  tokenizer: trigram remove_diacritics 1
   server: http://127.0.0.1:8765/mcp
   auth: on
   summarizer: anthropic model=your-cheap-model-id
   ...
   environment variables (values never shown):
     - JEZERO_SUMMARIZER_API_KEY: set
   database ready at /home/me/jezero/data/jezero.db
   ```

   Every variable should say `set`. When something is missing, `check-config` ends with a
   `problems:` list and exits with 1: enabled connectors without `summarizer.model` or
   its API key, a client whose `token_env` variable is not set, or `require_auth` on with
   no client configured.

## Start the services

Jezero runs as two systemd user units, in [`deploy/systemd`](../../deploy/systemd):

| Unit | What it does |
|---|---|
| `jezero-ingest.timer` | Starts `jezero-ingest.service` every 5 minutes (first run 5 minutes after boot). |
| `jezero-ingest.service` | Runs [`jezero run --due`](../reference/cli.md#jezero-run), then [`jezero health`](../reference/cli.md#jezero-health). A failing run does not skip the health check. Allows 3 hours for a first backfill. |
| `jezero-serve.service` | Runs [`jezero serve`](../reference/cli.md#jezero-serve) on `127.0.0.1:8765` and restarts it if it fails. |

1. Copy the units into your user's systemd folder:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp ~/jezero/deploy/systemd/jezero-{ingest.service,ingest.timer,serve.service} ~/.config/systemd/user/
   ```

2. If the checkout is not `~/jezero`, change the paths in the copies. For example, for a
   checkout in `/home/me/src/jezero`:

   ```bash
   sed -i 's|%h/jezero|/home/me/src/jezero|g' ~/.config/systemd/user/jezero-*.service
   ```

   With `uv tool install`, change each `ExecStart` to `%h/.local/bin/jezero ...` and keep
   `WorkingDirectory` on the folder that holds `jezero.toml`.

3. Start the timer and the server:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now jezero-ingest.timer jezero-serve.service
   ```

4. Keep them running when you are logged out. Without lingering, systemd stops your user
   units when your last session ends:

   ```bash
   loginctl enable-linger "$USER"
   ```

5. Check both:

   ```bash
   systemctl --user list-timers jezero-ingest.timer
   systemctl --user status jezero-serve.service
   ```

   The timer shows its next run within five minutes, and the server is `active
   (running)`. [Keep an eye on Jezero](monitor.md) explains the logs.

Only one ingestion runs at a time. If the timer fires while a long manual run is going,
the new run prints `{"skipped": "another run is in progress"}` and exits.

### Use cron instead

Without systemd user units, cron can run the ingestion. It cannot keep `jezero serve`
running, so run the server under your own process manager.

```bash
mkdir -p ~/jezero/data/logs
crontab -e
```

Add this line:

```cron
*/5 * * * * cd ~/jezero && .venv/bin/jezero run --due >> data/logs/run.log 2>&1; .venv/bin/jezero health >> data/logs/health.log 2>&1
```

## Reach it from other computers

`jezero serve` listens on `127.0.0.1`. To reach it from other computers, publish it
through a reverse proxy with TLS, on a private network such as a VPN or a mesh network.

1. Point a name on your private network at the server, for example
   `jezero.example.internal`, and give the proxy the whole host. The plugins call more
   than the MCP endpoint: `/mcp`, `/inbox/v1/...` for uploads, `/api/v1/overview` for the
   app and `/client/jezero-client.tar.gz` for the bundle. With
   [Caddy](https://caddyserver.com):

   ```caddy
   jezero.example.internal {
   	reverse_proxy 127.0.0.1:8765 {
   		flush_interval -1
   	}
   }
   ```

   `flush_interval -1` sends each response on as it is written instead of buffering it,
   which streamed MCP responses need. Other proxies have an equivalent setting (turn off
   response buffering).

2. Tell Jezero its public address in `jezero.toml`:

   ```toml
   [server]
   host = "127.0.0.1"
   port = 8765
   public_url = "https://jezero.example.internal/mcp"
   ```

   The server rejects requests whose `Host` header it does not know (protection against
   DNS rebinding), on every route: MCP, uploads, the key check, the dashboard and the
   bundle. `public_url` adds its host name; `127.0.0.1`, `localhost`, `[::1]` and
   `server.host` are always accepted. If the proxy also answers on other names, list them
   in [`allowed_hosts`](../reference/configuration.md#server):

   ```toml
   allowed_hosts = ["jezero.example.internal", "jezero"]
   ```

   Keep `path = "/mcp"` if you can. With another path, every computer must say so when it
   creates its key, with `jezero_client.py init --mcp-path PATH`.

3. Restart the server and check the name from another computer:

   ```bash
   systemctl --user restart jezero-serve.service
   ```

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://jezero.example.internal/client/jezero-client.tar.gz
   ```

   You should see `200`, or `404` if you have not built the bundle yet. A request with a
   name the server does not know gets:

   ```text
   Invalid Host header
   ```

   with status 421: add that name to `allowed_hosts`.

Without a proxy, you can bind `server.host` to the server's address on the private
network instead, if that network already encrypts traffic. Keep `require_auth = true`:
Jezero rejects a config that turns authentication off on any address other than
loopback, or together with `public_url` or `allowed_hosts` (behind a proxy, a loopback
server is reachable from wherever the proxy is). With `host = "0.0.0.0"`, list in `allowed_hosts` every address or name the
computers use to reach it (`"192.0.2.10:*"`): any other gets status 421, uploads
included.

> [!WARNING]
> Do not publish Jezero on the public internet. Every MCP and upload request needs a
> key, but the bundle route is open by design, and the library holds your work.

Next, give each computer its key: [Connect a second
computer](../getting-started/connect-a-computer.md) and [Manage computers and
keys](devices.md).

## Serve the plugin bundle

Computers install the plugins from a bundle that your server offers at
`https://YOUR-JEZERO/client/jezero-client.tar.gz`, with no key (it is public code).

1. Build it in the checkout:

   ```bash
   cd ~/jezero
   python3 scripts/build_clients.py
   ```

   It writes `dist/client/` and `dist/jezero-client.tar.gz`. It also refreshes the
   committed copy of the plugins (`plugins/` and the two `marketplace.json` files), which
   stays identical unless you changed `clients/`.

2. Leave [`server.client_bundle`](../reference/configuration.md#server) at its default,
   `dist/jezero-client.tar.gz`, relative to the config's folder, or point it at another
   file. The server reads the file on each request, so a rebuild needs no restart.

## Restart after configuration changes

`jezero serve` reads `[[clients]]` again by itself: at most once a second it checks
whether `jezero.toml` or the `.env` next to it changed, and if the file still loads, it
takes the new list of clients and the current values of their `token_env` variables. So [`jezero device approve`](../reference/cli.md#jezero-device-approve),
`device revoke`, `device rotate` and edits to a client's `sources` or `upload` need no
restart:

```text
approved laptop; the server picks up the new key within a second (backup: /home/me/jezero/jezero.toml.bak-20261022T034500Z)
```

If the edited file does not load, the server keeps only the clients whose `[[clients]]`
entry is unchanged since the last good load and refuses the keys of the ones you removed
or edited, until the file loads. It logs `config changed but could not be loaded (...);
keeping the previous clients whose [[clients]] entry is unchanged (...)`. Run
`jezero check-config` to find the error. You fix the file on the server itself, so no key
is needed to recover.

> [!NOTE]
> A variable set by the service manager (an `Environment=` line) wins over `.env` and is
> read only at start: change it, then restart the server.

Everything else the server reads once, when it starts. Restart it after any change to
`[server]`, `[search]`, `[inbox]` or `[storage]`:

```bash
systemctl --user restart jezero-serve.service
```

The ingestion starts a new process on every timer tick, so changes to connectors, the
summarizer, projects, redaction and the watchdog apply from the next run without a
restart.

## Back up

Back up four things:

| What | Where (defaults) | Why |
|---|---|---|
| The library | `data/jezero.db` ([`storage.path`](../reference/configuration.md#storage)) | Every item, with its kept original text, and each connector's cursor. |
| The inbox | `data/inbox/` ([`inbox.dir`](../reference/configuration.md#inbox)) | Sessions uploaded by other computers, as sent. |
| The config | `jezero.toml` | Clients, connectors and their options. |
| The secrets | `.env` | Keep this copy as safe as the server itself. |

1. Copy the library with SQLite's backup API, which is safe while Jezero is running:

   ```bash
   cd ~/jezero
   mkdir -p backups
   python3 -c "import sqlite3; sqlite3.connect('file:data/jezero.db?mode=ro', uri=True).backup(sqlite3.connect('backups/jezero.db'))"
   ```

   If the `sqlite3` command is installed,
   `sqlite3 data/jezero.db ".backup backups/jezero.db"` does the same.

2. Check the copy:

   ```bash
   python3 -c "import sqlite3; db = sqlite3.connect('file:backups/jezero.db?mode=ro', uri=True); print(db.execute('PRAGMA integrity_check').fetchone()[0], db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 'items')"
   ```

   You should see `ok` and the number of items, which should match `jezero stats`:

   ```text
   ok 3 items
   ```

3. Archive the inbox and the config:

   ```bash
   tar -czf backups/jezero-files.tar.gz data/inbox jezero.toml .env
   ```

> [!WARNING]
> Do not `cp` the database while Jezero runs. It uses WAL mode: recent writes live in
> `jezero.db-wal` until SQLite folds them into the main file, so a plain copy can miss
> them or be inconsistent. To copy files by hand, stop both units first and copy
> `jezero.db` together with `jezero.db-wal` and `jezero.db-shm`.

To restore, stop both units, move the current `jezero.db`, `jezero.db-wal` and
`jezero.db-shm` out of `data/`, copy the backup to `data/jezero.db`, and start the units
again. Sessions newer than the backup's cursors are collected again on the next run.

## Upgrade

1. Back up the library (see [Back up](#back-up)). Jezero has no schema migrations yet: if a
   release changes the schema, `jezero init` refuses the old database with `unsupported
   schema version` and you need the copy.

2. Get the new code and its dependencies:

   ```bash
   cd ~/jezero
   git pull
   uv sync
   ```

3. Read the new entries in [`CHANGELOG.md`](../../CHANGELOG.md) and check the config.
   Unknown keys are an error, so a renamed or removed key shows up here:

   ```bash
   .venv/bin/jezero check-config
   ```

4. Rebuild the plugin bundle, so new computers get the new plugins:

   ```bash
   python3 scripts/build_clients.py
   ```

5. Restart the server. The timer picks up the new code on its next run by itself.

   ```bash
   systemctl --user restart jezero-serve.service
   .venv/bin/jezero --version
   ```

The server's version (`jezero --version`) and the plugins' version are separate. When a
release bumps the plugins, update them on each computer as described in [Connect an
agent](connect-agents.md); the app's Agents view marks outdated plugins.

## Next steps

- [Keep an eye on Jezero](monitor.md): health checks, alerts and logs.
- [Add and tune sources](sources.md): bring in more agents and pilot each one first.
- [Manage computers and keys](devices.md): approve, limit and remove computers.
- [Security and privacy](../explanation/security.md): what the keys, the proxy and
  redaction protect.
