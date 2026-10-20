"""Watchdog: notice when a connector stops bringing anything in.

A connector is *stale* when it has not finished a successful run for longer than
`watchdog.max_silence_hours` (or has never succeeded although it is enabled). `jezero
health` reports every enabled connector and exits non-zero if any is stale or failing, so
it can run from a timer. If `watchdog.alert_webhook_env` names a variable holding a URL,
a JSON alert is POSTed there, at most once per `realert_hours` per connector.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import Config
from .connectors import CONNECTOR_TYPES
from .store import Store


@dataclass(slots=True)
class ConnectorHealth:
    connector: str
    status: str  # ok | stale | failing | never
    last_success_at: str | None
    hours_since_success: float | None
    last_error: str | None

    @property
    def healthy(self) -> bool:
        return self.status == "ok"


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
            status = "never"
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
                last_error=state.last_error,
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
) -> list[str]:
    """POST one alert per unhealthy connector not alerted recently. Returns names alerted."""
    env = config.watchdog.alert_webhook_env
    url = os.environ.get(env) if env else None
    if not url:
        return []
    now = now or datetime.now(UTC)
    gap = timedelta(hours=config.watchdog.realert_hours)
    sent = []
    with httpx.Client(timeout=15, transport=transport) as http:
        for entry in report:
            key = f"watchdog.last_alert.{entry.connector}"
            if entry.healthy:
                store.set_meta(key, None)  # recovered: the next problem alerts at once
                continue
            last = store.get_meta(key)
            if last and now - datetime.fromisoformat(last) < gap:
                continue
            text = (
                f"jezero: connector {entry.connector} is {entry.status}"
                + (
                    f" ({entry.hours_since_success} h since last success)"
                    if entry.hours_since_success
                    else ""
                )
                + (f": {entry.last_error[:300]}" if entry.last_error else "")
            )
            resp = http.post(url, json={"text": text, **asdict(entry)})
            if resp.status_code < 400:
                store.set_meta(key, now.isoformat())
                sent.append(entry.connector)
    return sent
