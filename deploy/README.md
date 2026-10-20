# Running Jezero unattended

`jezero run --due` runs every enabled connector whose `interval_minutes` has elapsed since
its last run; `jezero health` reports connectors that have not succeeded for
`watchdog.max_silence_hours` and exits non-zero (and POSTs an alert if a webhook is set).
Only one run happens at a time: a second one exits at once.

## systemd (user units)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/jezero-{ingest.service,ingest.timer,serve.service} ~/.config/systemd/user/
# edit the paths if the checkout is not ~/jezero
systemctl --user daemon-reload
systemctl --user enable --now jezero-ingest.timer jezero-serve.service
loginctl enable-linger "$USER"   # keep user timers running when you are logged out
```

Check it:

```bash
systemctl --user list-timers jezero-ingest.timer
journalctl --user -u jezero-ingest.service -n 50
```

## cron

```cron
*/5 * * * * cd ~/jezero && .venv/bin/jezero run --due >> data/logs/run.log 2>&1; .venv/bin/jezero health >> data/logs/health.log 2>&1
```

## Reaching the server from other machines

`jezero serve` listens on loopback. Publish it through a TLS reverse proxy on a private
network and set `server.public_url` to the public address, so the server accepts that
`Host` header. With Caddy, streaming responses need `flush_interval -1`:

```caddy
jezero.example.internal {
	reverse_proxy 127.0.0.1:8765 {
		flush_interval -1
	}
}
```

Then, on each machine, register it in the agent with that client's token, for example in
Claude Code:

```bash
claude mcp add --transport http --scope user jezero https://jezero.example.internal/mcp \
  --header "Authorization: Bearer $JEZERO_TOKEN"
```
