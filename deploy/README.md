# Running Jezero unattended

`jezero run --due` runs every enabled connector whose `interval_minutes` has elapsed since
its last run; `jezero health` reports connectors that have not succeeded for
`watchdog.max_silence_hours` and exits non-zero (and POSTs an alert if a webhook is set).
Only one run happens at a time: a second one exits at once.

## systemd (user units)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/jezero-ingest.{service,timer} ~/.config/systemd/user/
# edit the paths if the checkout is not ~/jezero
systemctl --user daemon-reload
systemctl --user enable --now jezero-ingest.timer
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
