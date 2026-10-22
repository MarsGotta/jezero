"""Command line interface: `jezero <command>`."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import __version__
from .config import (
    ClientConfig,
    Config,
    ConfigEditError,
    ConfigEditor,
    ConnectorConfig,
    load_config,
)
from .connectors import CONNECTOR_TYPES
from .ingest import IngestError, import_jsonl
from .redact import Redactor
from .store import Store, StoreError, sqlite_version


def _store(config: Config) -> Store:
    return Store(config.db_path, tokenizer=config.storage.tokenizer)


def cmd_init(config: Config, _: argparse.Namespace) -> int:
    store = _store(config)
    store.init()
    print(f"database ready at {store.path}")
    return 0


def cmd_check_config(config: Config, _: argparse.Namespace) -> int:
    print(f"config: {config.source_file}")
    print(f"database: {config.db_path}")
    print(f"sqlite: {'.'.join(map(str, sqlite_version()))}  tokenizer: {config.storage.tokenizer}")
    where = config.server.public_url or f"http://{config.server.host}:{config.server.port}"
    print(f"server: {where}{'' if config.server.public_url else config.server.path}")
    print(f"auth: {'on' if config.server.require_auth else 'OFF (loopback only)'}")
    print(f"summarizer: {config.summarizer.provider} model={config.summarizer.model or 'NOT SET'}")
    print("clients:")
    for client in config.clients:
        print(f"  - {client.name}: sources={client.sources}")
    print("connectors:")
    problems: list[str] = []
    for conn in config.connectors:
        kind = CONNECTOR_TYPES[conn.type]
        state = "enabled" if conn.enabled else "disabled"
        note = "" if kind.status == "available" else "  (not implemented yet)"
        print(f"  - {conn.name} [{conn.type}] {state}{note}")
    env = config.env_report()
    print("environment variables (values never shown):")
    for name, present in env.items():
        print(f"  - {name}: {'set' if present else 'missing'}")
    if config.server.require_auth and not config.clients:
        problems.append("require_auth is on but no clients are configured")
    runnable = [
        c.name
        for c in config.connectors
        if c.enabled and CONNECTOR_TYPES[c.type].status == "available"
    ]
    if runnable:
        # A library that only imports pre-summarised items needs neither.
        if not config.summarizer.model:
            problems.append(
                f"summarizer.model is not set, and {len(runnable)} connector(s) are enabled"
            )
        if not env.get(config.summarizer.api_key_env):
            problems.append(
                f"{config.summarizer.api_key_env} (summarizer.api_key_env) is not set, "
                f"and {len(runnable)} connector(s) are enabled"
            )
    for client in config.clients:
        if client.token_env and not env.get(client.token_env):
            problems.append(
                f"client {client.name}: {client.token_env} is not set, so it cannot log in"
            )
    if problems:
        print("problems:")
        for problem in problems:
            print(f"  - {problem}")
    return 1 if problems else 0


def cmd_stats(config: Config, _: argparse.Namespace) -> int:
    print(json.dumps(_store(config).stats(), indent=2, ensure_ascii=False))
    return 0


def cmd_search(config: Config, args: argparse.Namespace) -> int:
    hits = _store(config).search(
        args.query,
        source=args.source,
        project=args.project,
        limit=args.limit or config.search.default_limit,
        candidate_pool=config.search.candidate_pool,
        recency_half_life_days=config.search.recency_half_life_days,
        recency_weight=config.search.recency_weight,
    )
    for hit in hits:
        item = hit.item
        print(
            f"[{item.id}] {hit.score:7.3f}  {item.occurred_at:%Y-%m-%d}  {item.source}  "
            f"{item.project or '-'}  {item.title}"
        )
    if not hits:
        print("no results")
    return 0


def cmd_import_jsonl(config: Config, args: argparse.Namespace) -> int:
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)
    counts = import_jsonl(
        _store(config),
        Path(args.path),
        source=args.source,
        redactor=redactor,
        default_project=args.project,
        trust=args.trust,
    )
    print(json.dumps(dict(counts)))
    return 0


def cmd_new_token(_: Config | None, args: argparse.Namespace) -> int:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    print("Give this token to the client (shown once, never stored):")
    print(f"  {token}")
    print("Put this digest in the config:")
    print(f'  token_sha256 = "{digest}"')
    return 0


def _next_run(store: Store, conn: ConnectorConfig) -> datetime | None:
    """When `run --due` runs it next; None if it never ran (due now)."""
    last = store.get_state(conn.name).last_run_at
    if not last:
        return None
    return datetime.fromisoformat(last) + timedelta(minutes=conn.interval_minutes)


def _is_due(store: Store, conn: ConnectorConfig, now: datetime) -> bool:
    next_run = _next_run(store, conn)
    return next_run is None or now >= next_run


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    import fcntl

    store = _store(config)
    store.init()
    selected = [
        c
        for c in config.connectors
        if (args.connector is None and c.enabled) or c.name == args.connector
    ]
    if args.connector and not selected:
        print(f"error: no connector named {args.connector!r}", file=sys.stderr)
        return 2
    if not selected:
        print("no enabled connectors: enable one in jezero.toml")
        return 0
    # One run at a time: a timer firing during a long manual backfill just skips.
    with config.db_path.with_suffix(".run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "another run is in progress"}))
            return 0
        return _run_selected(config, store, selected, args)


def _run_selected(
    config: Config, store: Store, selected: list[ConnectorConfig], args: argparse.Namespace
) -> int:
    import asyncio

    from .pipeline import run_connector

    now = datetime.now(UTC)
    status = 0
    ran: list[str] = []
    not_due: dict[str, str] = {}
    for conn in selected:
        if CONNECTOR_TYPES[conn.type].status != "available":
            print(json.dumps({"connector": conn.name, "skipped": "not implemented yet"}))
            continue
        if args.due and not _is_due(store, conn, now):
            next_run = _next_run(store, conn)
            not_due[conn.name] = next_run.isoformat(timespec="seconds") if next_run else "now"
            continue
        ran.append(conn.name)
        try:
            report = asyncio.run(
                run_connector(
                    config, store, conn, limit=args.limit, sample=args.sample, dry_run=args.dry_run
                )
            )
        except Exception as exc:  # one broken source must not stop the others
            error = f"{type(exc).__name__}: {exc}"[:1000]
            if args.sample is None and not args.dry_run:  # pilots leave the state alone
                state = replace(
                    store.get_state(conn.name),
                    last_run_at=datetime.now(UTC).isoformat(),
                    last_error=error,
                )
                store.set_state(state)
            print(json.dumps({"connector": conn.name, "error": error}))
            status = 1
            continue
        print(json.dumps(report.to_dict(), ensure_ascii=False))
        status = status or (1 if report.failed else 0)
    if args.due:
        # One line even when nothing was due, so a timer's log shows it did run.
        print(json.dumps({"due": {"ran": ran, "not_due_until": not_due}}))
    return status


def cmd_retag(config: Config, args: argparse.Namespace) -> int:
    """Re-apply the source's project hints and the project aliases to stored items. No LLM
    calls."""
    import fcntl

    from .pipeline import build_connector, connector_rules
    from .store import ConnectorState

    conn = next((c for c in config.connectors if c.name == args.connector), None)
    if conn is None:
        print(f"error: no connector named {args.connector!r}", file=sys.stderr)
        return 2
    store = _store(config)
    with config.db_path.with_suffix(".run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "another run is in progress"}))
            return 0
        batch = build_connector(config, conn).collect(ConnectorState(name=conn.name))
        rules = connector_rules(config, conn)
        changed = 0
        for doc in batch.docs:
            current = store.project_of(conn.name, doc.external_id)
            # The source's own hint wins; otherwise the stored tag (the summarizer's guess)
            # goes through the aliases, so a new alias fixes old guesses too.
            wanted = doc.project_hint or (rules.rename(current) if current else None)
            if not wanted or current is None or current == wanted:
                continue
            if args.dry_run:
                changed += 1
            else:
                changed += int(store.set_project(conn.name, doc.external_id, wanted))
    report = {"connector": conn.name, "docs": len(batch.docs), "retagged": changed}
    print(json.dumps(report | {"dry_run": bool(args.dry_run)}))
    return 0


def cmd_resummarize(config: Config, args: argparse.Namespace) -> int:
    """Summarise stored items again from their original text (ids from a file or a list)."""
    import asyncio
    import fcntl

    from .pipeline import resummarize_items

    raw = args.ids.split(",") if args.ids else []
    if args.ids_file:
        raw += Path(args.ids_file).read_text().split()
    ids = sorted({int(x) for x in raw if x.strip()})
    if not ids:
        print("error: no item ids given", file=sys.stderr)
        return 2
    store = _store(config)
    with config.db_path.with_suffix(".run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "another run is in progress"}))
            return 0
        report = asyncio.run(resummarize_items(config, store, ids))
    print(json.dumps(report.to_dict(), ensure_ascii=False))
    return 1 if report.failed else 0


def cmd_redact_stored(config: Config, args: argparse.Namespace) -> int:
    """Re-apply the current redaction rules to items already stored (no LLM calls)."""
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)

    def redact(text: str) -> tuple[str, int]:
        result = redactor.redact(text)
        return result.text, result.count

    store = _store(config)
    store.init()
    items, total = store.redact_stored(redact, dry_run=args.dry_run)
    verb = "would redact" if args.dry_run else "redacted"
    print(f"{verb} {total} secrets in {items} items")
    return 0


def cmd_device_approve(config: Config, args: argparse.Namespace) -> int:
    """Register a device key (its SHA-256 digest) and its inbox connectors (disabled)."""
    import fnmatch
    import re

    name, digest = args.name, args.digest.removeprefix("sha256:").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        print("error: device name must be lowercase letters, digits, - or _", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        print("error: digest must be the 64-char SHA-256 printed by the client", file=sys.stderr)
        return 2
    if any(c.name == name for c in config.clients):
        print(f"error: a client named {name!r} already exists", file=sys.stderr)
        return 1
    if config.source_file is None:
        print("error: no config file to write to", file=sys.stderr)
        return 1
    inbox = config.inbox_dir / name
    prefix = args.source_prefix
    if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", prefix):
        print("error: --source-prefix must be a short lowercase word", file=sys.stderr)
        return 2
    readable = list(args.sources)
    own = f"{prefix}-*-{name}"
    if not args.read_only and not any(fnmatch.fnmatch(own, s) for s in readable):
        readable.append(own)  # a device always sees its own sessions
    sources = ", ".join(json.dumps(s) for s in readable)
    block = [
        "",
        "[[clients]]",
        f"# Device {name}: reads Jezero"
        + (" only." if args.read_only else " and uploads its sessions to the inbox."),
        f'name = "{name}"',
        f'token_sha256 = "{digest}"',
        f"sources = [{sources}]",
        f"upload = {'false' if args.read_only else 'true'}",
    ]
    stores = {
        "claude-code": ("claude_code", "projects_dir", inbox / "claude-code" / "projects"),
        "codex": ("codex", "sessions_dir", inbox / "codex" / "sessions"),
        "opencode": ("opencode", "export_dir", inbox / "opencode" / "sessions"),
        "hermes": ("hermes", "export_dir", inbox / "hermes" / "sessions"),
        "antigravity": ("antigravity", "transcripts_dir", inbox / "antigravity" / "brain"),
    }
    existing = {c.name for c in config.connectors}
    kept: list[str] = []
    for agent, (ctype, option, folder) in ({} if args.read_only else stores).items():
        folder.mkdir(parents=True, exist_ok=True)
        if f"{prefix}-{agent}-{name}" in existing:
            kept.append(f"{prefix}-{agent}-{name}")  # left by `device revoke`, cursor intact
            continue
        block += [
            "",
            "[[connectors]]",
            f"# Sessions uploaded by {name}. Disabled until the pilot passes.",
            f'name = "{prefix}-{agent}-{name}"',
            f'type = "{ctype}"',
            "enabled = false",
            "interval_minutes = 15",
            "[connectors.options]",
            f'{option} = "{folder}"',
        ]
    try:
        editor = _config_editor(config)
        editor.append(block)
        _, backup = editor.commit()
    except ConfigEditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"approved {name}; the server picks up the new key within a second (backup: {backup})")
    if kept:
        print(f"kept its existing connectors (with their cursors): {', '.join(kept)}")
    return 0


def _device_connectors(config: Config, name: str) -> list[ConnectorConfig]:
    """Connectors that read the inbox folder of device `name` (as `device approve` adds)."""
    folder = (config.inbox_dir / name).resolve()
    base = config.source_file.parent if config.source_file else Path.cwd()
    found: list[ConnectorConfig] = []
    for conn in config.connectors:
        for value in conn.options.values():
            if not isinstance(value, str) or not value:
                continue
            path = Path(value).expanduser()
            path = (path if path.is_absolute() else base / path).resolve()
            if path == folder or folder in path.parents:
                found.append(conn)
                break
    return found


def _client_kind(client: ClientConfig) -> str:
    return "token_env" if client.token_env else "device"


def _table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _config_editor(config: Config) -> ConfigEditor:
    if config.source_file is None:
        raise ConfigEditError("no config file to write to")
    return ConfigEditor(config.source_file)


def cmd_device_list(config: Config, args: argparse.Namespace) -> int:
    """List the clients: devices (keys by digest) and token_env clients."""
    entries = []
    for client in config.clients:
        kind = _client_kind(client)
        connectors = _device_connectors(config, client.name) if kind == "device" else []
        inbox = config.inbox_dir / client.name if kind == "device" and client.upload else None
        entries.append(
            {
                "name": client.name,
                "kind": kind,
                "upload": client.upload,
                "sources": list(client.sources),
                "inbox": str(inbox) if inbox else None,
                "connectors": [{"name": c.name, "enabled": c.enabled} for c in connectors],
            }
        )
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print("no clients configured")
        return 0
    rows = [
        [
            e["name"],
            e["kind"],
            "yes" if e["upload"] else "no",
            ", ".join(e["sources"]),
            e["inbox"] or "-",
        ]
        for e in entries
    ]
    _table(rows, ["NAME", "KIND", "UPLOAD", "SOURCES", "INBOX"])
    for entry in entries:
        if entry["connectors"]:
            listed = ", ".join(
                f"{c['name']} ({'enabled' if c['enabled'] else 'disabled'})"
                for c in entry["connectors"]
            )
            print(f"{entry['name']} connectors: {listed}")
    return 0


def cmd_device_revoke(config: Config, args: argparse.Namespace) -> int:
    """Remove a client's [[clients]] block; disable (or remove) its inbox connectors."""
    import re

    name = args.name
    if not any(c.name == name for c in config.clients):
        print(f"error: no client named {name!r} (see `jezero device list`)", file=sys.stderr)
        return 2
    connectors = _device_connectors(config, name)
    try:
        editor = _config_editor(config)
        block = editor.find("clients", name)
        if block is None:
            raise ConfigEditError(f"could not find the [[clients]] block of {name!r}")
        editor.remove(block, comment_above=rf"\s*#\s*Device {re.escape(name)}\b")
        for conn in connectors:
            conn_block = editor.find("connectors", conn.name)
            if conn_block is None:
                raise ConfigEditError(f"could not find the [[connectors]] block of {conn.name!r}")
            if args.remove_connectors:
                editor.remove(conn_block)
            else:
                editor.set_value(conn_block, "enabled", "false")
        _, backup = editor.commit()
    except ConfigEditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"revoked {name}: its key stops working within a second (backup: {backup})")
    if connectors:
        names = ", ".join(c.name for c in connectors)
        if args.remove_connectors:
            print(f"removed its connectors: {names}")
        else:
            print(f"disabled its connectors (kept, with their cursors): {names}")
    sources = {c.name for c in connectors}
    if sources and config.db_path.exists():
        counts = _store(config).stats()["sources"]
        assert isinstance(counts, dict)
        stored = sum(int(counts[s]["items"]) for s in sources if s in counts)
        print(f"items already stored are kept: {stored} from its connectors")
    else:
        print("items already stored are kept")
    return 0


def cmd_device_rotate(config: Config, args: argparse.Namespace) -> int:
    """Replace a device's token_sha256 in place."""
    import re

    name, digest = args.name, args.digest.removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        print("error: digest must be the 64-char SHA-256 printed by the client", file=sys.stderr)
        return 2
    client = next((c for c in config.clients if c.name == name), None)
    if client is None:
        print(f"error: no client named {name!r} (see `jezero device list`)", file=sys.stderr)
        return 2
    if client.token_sha256 is None:
        print(
            f"error: {name} reads its key from {client.token_env}; change that variable instead",
            file=sys.stderr,
        )
        return 1
    try:
        editor = _config_editor(config)
        block = editor.find("clients", name)
        if block is None:
            raise ConfigEditError(f"could not find the [[clients]] block of {name!r}")
        editor.set_value(block, "token_sha256", f'"{digest}"')
        _, backup = editor.commit()
    except ConfigEditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rotated {name}: the old key stops working within a second (backup: {backup})")
    return 0


def _short_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def cmd_connector_list(config: Config, args: argparse.Namespace) -> int:
    """Connectors with their state (last run, success, error) and stored items."""
    from .store import ConnectorState

    states: dict[str, ConnectorState] = {}
    counts: dict[str, dict[str, object]] = {}
    if config.db_path.exists():
        store = _store(config)
        states = {s.name: s for s in store.all_states()}
        sources = store.stats()["sources"]
        assert isinstance(sources, dict)
        counts = sources
    entries = []
    for conn in config.connectors:
        state = states.get(conn.name, ConnectorState(name=conn.name))
        entries.append(
            {
                "name": conn.name,
                "type": conn.type,
                "enabled": conn.enabled,
                "interval_minutes": conn.interval_minutes,
                "last_run_at": state.last_run_at,
                "last_success_at": state.last_success_at,
                "last_error": state.last_error,
                "items": int(counts.get(conn.name, {}).get("items", 0)),  # type: ignore[call-overload]
            }
        )
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print("no connectors configured")
        return 0
    rows = [
        [
            str(e["name"]),
            str(e["type"]),
            "yes" if e["enabled"] else "no",
            f"{e['interval_minutes']}m",
            _short_time(e["last_run_at"]),  # type: ignore[arg-type]
            _short_time(e["last_success_at"]),  # type: ignore[arg-type]
            str(e["items"]),
            (str(e["last_error"])[:60] if e["last_error"] else "-"),
        ]
        for e in entries
    ]
    _table(rows, ["NAME", "TYPE", "ENABLED", "EVERY", "LAST RUN", "LAST SUCCESS", "ITEMS", "ERROR"])
    return 0


def cmd_connector_reset(config: Config, args: argparse.Namespace) -> int:
    """Forget a connector's cursor so its next run reads the source from the start."""
    import fcntl

    conn = next((c for c in config.connectors if c.name == args.name), None)
    if conn is None:
        print(f"error: no connector named {args.name!r}", file=sys.stderr)
        return 2
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: pass --yes to confirm (no terminal to ask)", file=sys.stderr)
            return 2
        answer = input(f"Forget the cursor of {conn.name}, so it reads its source again? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 1
    if not config.db_path.exists():
        print(f"{conn.name} has no cursor yet: nothing to reset")
        return 0
    store = _store(config)
    with config.db_path.with_suffix(".run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("error: a run is in progress; try again when it ends", file=sys.stderr)
            return 1
        had = store.reset_cursor(conn.name)
    if not had:
        print(f"{conn.name} has no cursor yet: its next run reads everything anyway")
        return 0
    print(
        f"reset {conn.name}: its next run reads the whole source again. Stored items are "
        "kept; units whose redacted text is unchanged are skipped by content hash, "
        "without summarizer calls."
    )
    return 0


def cmd_health(config: Config, _: argparse.Namespace) -> int:
    from .watchdog import alert, evaluate, heartbeat

    store = _store(config)
    store.init()
    report = evaluate(config, store)
    for entry in report:
        print(json.dumps({**asdict(entry)}, ensure_ascii=False))
    alert_errors: list[str] = []
    sent = alert(config, store, report, errors=alert_errors)
    if sent:
        print(json.dumps({"alerted": sent}))
    for problem in alert_errors:  # logged, never fatal: the heartbeat still goes out
        print(json.dumps({"alert_error": problem}))
    try:
        pinged = heartbeat(config, report)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:  # the monitor notices the missing ping
        pinged = None
        print(json.dumps({"heartbeat_error": type(exc).__name__}))
    if pinged:
        print(json.dumps({"heartbeat": pinged}))
    return 0 if all(e.healthy for e in report) else 1


def cmd_serve(config: Config, _: argparse.Namespace) -> int:
    import uvicorn

    from .server import build_server

    store = _store(config)
    store.init()
    _, app = build_server(config, store)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jezero", description=__doc__)
    parser.add_argument("--version", action="version", version=f"jezero {__version__}")
    parser.add_argument("-c", "--config", help="path to jezero.toml")
    sub = parser.add_subparsers(dest="command")

    from .ui.commands import add_commands

    add_commands(sub)

    sub.add_parser("init", help="create or verify the database").set_defaults(fn=cmd_init)
    sub.add_parser(
        "check-config", help="validate the config and report missing env vars"
    ).set_defaults(fn=cmd_check_config)
    sub.add_parser("stats", help="item counts per source").set_defaults(fn=cmd_stats)

    p = sub.add_parser("search", help="search from the terminal (admin: sees every source)")
    p.add_argument("query", help="words to look for (matching is literal)")
    p.add_argument("--source", help="only this source")
    p.add_argument("--project", help="only this project")
    p.add_argument("--limit", type=int, help="maximum results (default: search.default_limit)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("import-jsonl", help="import pre-summarised items from a JSONL file")
    p.add_argument("path", help="JSONL file, one item per line")
    p.add_argument("--source", required=True, help="source name for the imported items")
    p.add_argument("--project", help="project for items that do not name one")
    p.add_argument("--trust", choices=["own", "external"], default="own", help="trust of the items")
    p.set_defaults(fn=cmd_import_jsonl)

    p = sub.add_parser("new-token", help="generate a client token and its SHA-256 digest")
    p.set_defaults(fn=cmd_new_token, needs_config=False)

    p = sub.add_parser("run", help="run connectors once: collect, summarise, store")
    p.add_argument("--connector", help="only this connector (default: every enabled one)")
    p.add_argument("--limit", type=int, help="process at most N units, oldest first")
    p.add_argument("--sample", type=int, help="process the newest N units, cursor untouched")
    p.add_argument("--dry-run", action="store_true", help="count work without calling the LLM")
    p.add_argument(
        "--due", action="store_true", help="only connectors whose interval_minutes has elapsed"
    )
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("redact-stored", help="apply the current redaction rules to stored items")
    p.add_argument("--dry-run", action="store_true", help="count secrets without writing")
    p.set_defaults(fn=cmd_redact_stored)

    p = sub.add_parser("device", help="devices that upload sessions (Jezero plugins)")
    dsub = p.add_subparsers(dest="device_command", required=True)
    d = dsub.add_parser("approve", help="accept a device key printed by `jezero_client init`")
    d.add_argument("name", help="name of the device, as given to `jezero_client init`")
    d.add_argument("digest", help="sha256 of the device key, as printed by the client")
    d.add_argument("--sources", nargs="+", default=["agent-*"], help="what it may read")
    d.add_argument(
        "--source-prefix",
        default="agent",
        help="prefix of its sources; anything but 'agent' keeps them out of agent-* readers",
    )
    d.add_argument("--read-only", action="store_true", help="it may read but not upload")
    d.set_defaults(fn=cmd_device_approve)
    d = dsub.add_parser("list", help="list clients: devices and token_env clients")
    d.add_argument("--json", action="store_true", help="print JSON instead of a table")
    d.set_defaults(fn=cmd_device_list)
    d = dsub.add_parser(
        "revoke",
        help="remove a client's key; its inbox connectors are disabled, stored items kept",
    )
    d.add_argument("name", help="client to remove, as shown by `jezero device list`")
    d.add_argument(
        "--remove-connectors",
        action="store_true",
        help="remove its inbox connectors from the config instead of disabling them",
    )
    d.set_defaults(fn=cmd_device_revoke)
    d = dsub.add_parser("rotate", help="replace a device key; the old key stops working")
    d.add_argument("name", help="device whose key changes")
    d.add_argument("digest", help="sha256 of the new device key, as printed by the client")
    d.set_defaults(fn=cmd_device_rotate)

    p = sub.add_parser("connector", help="inspect connectors or make one read its source again")
    csub = p.add_subparsers(dest="connector_command", required=True)
    c = csub.add_parser("list", help="connectors with last run, last success, error and items")
    c.add_argument("--json", action="store_true", help="print JSON instead of a table")
    c.set_defaults(fn=cmd_connector_list)
    c = csub.add_parser(
        "reset",
        help="forget a connector's cursor so its next run reads the source from the start; "
        "stored items are kept and unchanged ones are skipped by content hash (no LLM calls)",
    )
    c.add_argument("name", help="connector to reset")
    c.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    c.set_defaults(fn=cmd_connector_reset)

    p = sub.add_parser("resummarize", help="summarise stored items again from their original")
    p.add_argument("--ids", help="comma-separated item ids")
    p.add_argument("--ids-file", help="file with item ids, one per line")
    p.set_defaults(fn=cmd_resummarize)

    p = sub.add_parser(
        "retag", help="re-apply the source's project hints and the project aliases, without the LLM"
    )
    p.add_argument("--connector", required=True, help="connector whose items are retagged")
    p.add_argument("--dry-run", action="store_true", help="count changes without writing")
    p.set_defaults(fn=cmd_retag)

    sub.add_parser(
        "health", help="report stale or failing connectors (exit 1 if any); alert if set"
    ).set_defaults(fn=cmd_health)

    sub.add_parser("serve", help="run the MCP server").set_defaults(fn=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .ui.app import run

            return run()
        parser.print_help()
        return 0
    try:
        config = load_config(args.config) if getattr(args, "needs_config", True) else None
        return args.fn(config, args)
    except (FileNotFoundError, StoreError, IngestError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:  # SummarizerError and friends
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"invalid config:\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
