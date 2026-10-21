# Keep an eye on Jezero

Know when a source stops bringing in sessions, get told about it before you notice the
gap, and find out why. This guide covers `jezero health`, alerts, `jezero stats`, the logs
and the quick checks in the app.

## Before you start

- Jezero running on a schedule, as in [Run Jezero as a service](deploy.md). The commands
  below run in the folder that holds `jezero.toml` (`~/jezero` in that guide), with
  `.venv/bin/jezero` or `uv run jezero`.

## Check health now

1. Run the health check:

   ```bash
   jezero health
   ```

   You should see one line per enabled connector:

   ```json
   {"connector": "agent-claude-code", "status": "ok", "last_success_at": "2026-10-21T21:23:10.649350+00:00", "hours_since_success": 0.0, "last_error": null}
   ```

2. Read the exit code: `0` when every enabled connector is `ok`, `1` when any is not, `2`
   when the config or the database cannot be read.

   ```bash
   echo $?
   ```

`jezero health` only reads what the last runs recorded; it does not run any connector.
Disabled connectors and planned types are left out. The ingestion unit runs it after
every `jezero run --due`, so a problem also shows up as a failed
`jezero-ingest.service`:

```bash
systemctl --user --failed
```

### What each status means

| Status | Meaning |
|---|---|
| `ok` | The connector finished a run without errors within the last `watchdog.max_silence_hours`. |
| `failing` | It succeeded within that window, but its latest run recorded an error or a warning, shown in `last_error`. |
| `stale` | It has not finished a successful run for longer than `max_silence_hours` (24 by default). |
| `never` | It is enabled but has never finished a successful run. |

A run that finds nothing new still counts as a success. So `stale` never means "no new
sessions": it means the runs stopped, or every run since then failed.

## Fix what it reports

1. Run the connector by hand and read its report:

   ```bash
   jezero run --connector agent-claude-code
   ```

   A broken connector prints its error and exits with 1, for example:

   ```json
   {"connector": "agent-claude-code", "error": "FileNotFoundError: Claude Code projects directory not found: /home/me/.claude/projects"}
   ```

2. Fix the cause. The usual ones:

   | `last_error` or symptom | Likely cause | What to do |
   |---|---|---|
   | `FileNotFoundError: ... not found` | The source folder or database moved, or the path option is wrong. | Fix the option in `jezero.toml` (see [Connectors](../reference/connectors.md)). |
   | `summarizer failed after retries`, `HTTP 401`, `HTTP 404` | The model endpoint is down, the key is missing or wrong, or the model name is wrong. | Run `jezero check-config` (every variable must say `set`) and check `[summarizer]`. |
   | A warning about a format the connector cannot read | The agent changed its storage format. | Update Jezero; until then the cursor stays put, so nothing is lost. |
   | `stale` with no error | The timer is not running. | Check `systemctl --user list-timers jezero-ingest.timer` and lingering (see [Start the services](deploy.md#start-the-services)). |

3. Run the connector again. A clean run clears `last_error`, and `jezero health` shows
   `ok`.

If your timer runs less often than once a day, raise
[`watchdog.max_silence_hours`](../reference/configuration.md#watchdog) above the gap
between runs, or every connector turns `stale` between them.

## Get alerts

Two mechanisms, both optional, both set in `[watchdog]`. Use the heartbeat if you can:
it also catches the case where nothing runs at all.

### Heartbeat to a push monitor

1. Create a push monitor in your uptime tool (one that expects a request every few
   minutes and alerts when they stop) and copy its URL.

2. Name a variable for it in `jezero.toml`, and put the URL in `.env` under that name:

   ```toml
   [watchdog]
   heartbeat_url_env = "JEZERO_HEARTBEAT_URL"
   ```

3. Run `jezero health`. Every run calls the URL with `status=up` when every connector is
   `ok`, or `status=down` with the unhealthy ones in `msg`:

   ```text
   GET /push?status=up&msg=1+connectors+ok&ping=
   GET /push?status=down&msg=agent-claude-code+stale&ping=
   ```

   The health check prints `{"heartbeat": "up"}` or `{"heartbeat": "down"}`. If the
   monitor cannot be reached it prints `{"heartbeat_error": "ConnectError"}` and goes
   on: the monitor notices the missing ping anyway.

Set the monitor's interval a little above the timer's 5 minutes, so one slow run does not
raise an alarm.

### Webhook alert

1. Name a variable for the webhook URL in `jezero.toml`, and put the URL in `.env`:

   ```toml
   [watchdog]
   alert_webhook_env = "JEZERO_ALERT_WEBHOOK_URL"
   realert_hours = 12
   ```

2. Jezero POSTs one JSON message per unhealthy connector. `text` is a ready-made line,
   which chat tools that accept a `text` field show as is:

   ```json
   {"text": "jezero: connector agent-claude-code is stale (7053.39 h since last success)", "connector": "agent-claude-code", "status": "stale", "last_success_at": "2026-01-01T00:00:00+00:00", "hours_since_success": 7053.39, "last_error": null}
   ```

   `jezero health` then prints `{"alerted": ["agent-claude-code"]}`.

The same connector alerts again at most every `realert_hours`. When it recovers, the
counter resets, so its next problem alerts at once. If the webhook answers with an error
status, the alert is not counted as sent and goes out again on the next check.

## See what came in

`jezero stats` counts the items per source and shows the newest one:

```bash
jezero stats
```

You should see:

```json
{
  "items": 3,
  "sources": {
    "agent-claude-code": {
      "items": 3,
      "latest": "2026-10-01T10:01:00+00:00"
    }
  }
}
```

`latest` is when the newest session happened, not when Jezero stored it. A source can be
healthy and still have an old `latest`, if nobody used that agent.

## Read the logs

1. The ingestion log: each run prints one JSON line per connector it ran (connectors that
   are not due print nothing), then the health lines.

   ```bash
   journalctl --user -u jezero-ingest.service -n 50
   ```

   A run report looks like this:

   ```json
   {"connector": "agent-claude-code", "docs": 3, "unchanged": 2, "stored": 1, "failed": 0, "deferred_sessions": 0, "redactions": 3, "input_chars": 1091, "input_tokens": 900, "output_tokens": 120, "dry_run": false, "errors": [], "warnings": []}
   ```

   `docs` is what the connector collected, `unchanged` what was already stored as is,
   `stored` what the summarizer wrote, `failed` what it could not summarise (the first
   errors follow in `errors`), and `deferred_sessions` the sessions still too recent to
   read. The token counts tell you what the run cost.

2. The server log: one line per request with its status, plus one line per upload.

   ```bash
   journalctl --user -u jezero-serve.service -f
   ```

   ```text
   INFO:     127.0.0.1:58152 - "PUT /inbox/v1/claude-code/projects/-home-me-projects-atlas/sess-9.jsonl HTTP/1.1" 200 OK
   INFO:     127.0.0.1:58162 - "GET /inbox/v1/ping HTTP/1.1" 401 Unauthorized
   ```

   `401` is an unknown or missing key (often a device approved without restarting the
   server), `403` a read-only key trying to upload, `421` a host name missing from
   `allowed_hosts`.

Jezero writes no log files of its own. With the cron line from the deploy guide, the
output goes to `data/logs/run.log` and `data/logs/health.log`.

## Quick checks from any computer

- The app's **Home** view lists every source your key may read, with a green dot when its
  newest item is less than a day old, amber within a week, grey after that. See [Use the
  Jezero app](app.md).
- `jezero agents` lists the agents on the computer you run it on, with their plugin
  version and the date of the newest item from that agent's device sources
  (`agent-claude-code-laptop`, `agent-codex-work-mac` and so on) that this computer's key
  may read:

  ```bash
  jezero agents
  ```

Both show the newest *item*. A session reaches Jezero only after it has been quiet for
`settle_minutes` (30 by default) and the connector's next run, so a gap of up to an hour
is normal.

## Next steps

- [Troubleshooting](troubleshooting.md): symptoms, causes and fixes beyond the health
  check.
- [Add and tune sources](sources.md): pilot a new source before it reaches the watchdog.
- [Run Jezero as a service](deploy.md): the units, backups and upgrades.
