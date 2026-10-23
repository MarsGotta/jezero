"""Watchdog: notice when a connector stops bringing anything in.

A connector is *stale* when it has not finished a successful run for longer than
`watchdog.max_silence_hours` (or has never succeeded although it is enabled). `jezero
health` reports every enabled connector and exits non-zero if any is stale or failing, so
it can run from a timer. If `watchdog.alert_webhook_env` names a variable holding a URL,
a JSON alert is POSTed there, at most once per `realert_hours` per connector. If
`watchdog.heartbeat_url_env` is set, every check also pings that push URL with
`status=up|down`: the monitor on the other end alerts when the pings stop, which also
covers the scheduler itself dying.

Everything the watchdog reports (a connector's last error, the alert text and body, a
webhook failure) goes through the redactor first, and the value of every secret the
config references by environment variable (the summarizer's API key, client keys,
connector secrets, the webhook and heartbeat URLs) is replaced wherever it appears.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import Config
from .connectors import CONNECTOR_TYPES
from .redact import Redactor
from .store import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectorHealth:
    connector: str
    status: str  # ok | stale | failing | never (enabled, no run yet)
    last_success_at: str | None
    hours_since_success: float | None
    last_error: str | None

    @property
    def healthy(self) -> bool:
        return self.status == "ok"


SECRET_LABEL = "[REDACTED:secret]"  # noqa: S105  a placeholder, not a password
_MIN_SECRET_CHARS = 6
"""Shorter values are not replaced literally (they would match ordinary words)."""


def scrub(config: Config, text: str | None) -> str | None:
    """`text` without secrets: the redactor's patterns (always on here, whatever
    `redaction.enabled` says) and the literal value of every referenced secret."""
    if not text:
        return text
    secrets = [
        value
        for name in config.env_report()
        if (value := os.environ.get(name)) and len(value) >= _MIN_SECRET_CHARS
    ]
    for value in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(value, SECRET_LABEL)
    return Redactor(config.redaction.extra_patterns).redact(text).text


def evaluate(config: Config, store: Store, *, now: datetime | None = None) -> list[ConnectorHealth]:
    now = now or datetime.now(UTC)
    limit = timedelta(hours=config.watchdog.max_silence_hours)
    report = []
    for conn in config.connectors:
        if not conn.enabled or CONNECTOR_TYPES[conn.type].status != "available":
            continue
        state = store.get_state(conn.name)
        since = None
        if state.last_success_at:
            since = now - datetime.fromisoformat(state.last_success_at)
        if since is None:
            # Never succeeded: "failing" when a run already tried and left an error.
            status = "failing" if state.last_error else "never"
        elif since > limit:
            status = "stale"
        elif state.last_error:
            status = "failing"
        else:
            status = "ok"
        report.append(
            ConnectorHealth(
                connector=conn.name,
                status=status,
                last_success_at=state.last_success_at,
                hours_since_success=round(since.total_seconds() / 3600, 2) if since else None,
                last_error=scrub(config, state.last_error),
            )
        )
    return report


def alert(
    config: Config,
    store: Store,
    report: list[ConnectorHealth],
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
    errors: list[str] | None = None,
) -> list[str]:
    """POST one alert per unhealthy connector not alerted recently. Returns names alerted.

    A webhook that cannot be reached is logged (and appended to `errors`, if given) and
    stops the alerts for this check; it never raises, so the heartbeat still goes out.
    """
    env = config.watchdog.alert_webhook_env
    url = os.environ.get(env) if env else None
    if not url:
        return []
    now = now or datetime.now(UTC)
    gap = timedelta(hours=config.watchdog.realert_hours)
    sent = []
    unreachable = False
    with httpx.Client(timeout=15, transport=transport) as http:
        for entry in report:
            key = f"watchdog.last_alert.{entry.connector}"
            if entry.healthy:
                store.set_meta(key, None)  # recovered: the next problem alerts at once
                continue
            last = store.get_meta(key)
            if unreachable or (last and now - datetime.fromisoformat(last) < gap):
                continue
            error = scrub(config, entry.last_error)
            text = (
                f"jezero: connector {entry.connector} is {entry.status}"
                + (
                    f" ({entry.hours_since_success} h since last success)"
                    if entry.hours_since_success
                    else ""
                )
                + (f": {error[:300]}" if error else "")
            )
            body = {**asdict(entry), "last_error": error, "text": text}
            try:
                resp = http.post(url, json=body)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                problem = f"alert webhook failed: {type(exc).__name__}"
                # The exception text may hold the webhook URL, itself a secret.
                log.warning("%s: %s", problem, scrub(config, str(exc)))
                if errors is not None:
                    errors.append(problem)
                unreachable = True  # no more posts this check; the next one tries again
                continue
            if resp.status_code < 400:
                store.set_meta(key, now.isoformat())
                sent.append(entry.connector)
    return sent


def heartbeat(
    config: Config,
    report: list[ConnectorHealth],
    *,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Ping the push URL with the overall status. Returns the status sent, or None."""
    env = config.watchdog.heartbeat_url_env
    url = os.environ.get(env) if env else None
    if not url:
        return None
    bad = [e for e in report if not e.healthy]
    status = "down" if bad else "up"
    msg = (
        "; ".join(f"{e.connector} {e.status}" for e in bad)
        if bad
        else f"{len(report)} connectors ok"
    )
    with httpx.Client(timeout=15, transport=transport) as http:
        msg = scrub(config, msg) or ""
        resp = http.get(url, params={"status": status, "msg": msg[:250], "ping": ""})
    return status if resp.status_code < 400 else None
