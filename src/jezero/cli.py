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

from pydantic import ValidationError

from . import __version__
from .config import Config, ConnectorConfig, load_config
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
    problems = 0
    for conn in config.connectors:
        kind = CONNECTOR_TYPES[conn.type]
        state = "enabled" if conn.enabled else "disabled"
        note = "" if kind.status == "available" else "  (not implemented yet)"
        print(f"  - {conn.name} [{conn.type}] {state}{note}")
    print("environment variables (values never shown):")
    for name, present in config.env_report().items():
        print(f"  - {name}: {'set' if present else 'missing'}")
    if config.server.require_auth and not config.clients:
        print("ERROR: require_auth is on but no clients are configured")
        problems += 1
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


def _is_due(store: Store, conn: ConnectorConfig, now: datetime) -> bool:
    last = store.get_state(conn.name).last_run_at
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(minutes=conn.interval_minutes)


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
    for conn in selected:
        if CONNECTOR_TYPES[conn.type].status != "available":
            print(json.dumps({"connector": conn.name, "skipped": "not implemented yet"}))
            continue
        if args.due and not _is_due(store, conn, now):
            continue
        try:
            report = asyncio.run(
                run_connector(
                    config, store, conn, limit=args.limit, sample=args.sample, dry_run=args.dry_run
                )
            )
        except Exception as exc:  # one broken source must not stop the others
            state = replace(
                store.get_state(conn.name),
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=f"{type(exc).__name__}: {exc}"[:1000],
            )
            store.set_state(state)
            print(json.dumps({"connector": conn.name, "error": state.last_error}))
            status = 1
            continue
        print(json.dumps(report.to_dict(), ensure_ascii=False))
        status = status or (1 if report.failed else 0)
    return status


def cmd_health(config: Config, _: argparse.Namespace) -> int:
    from .watchdog import alert, evaluate

    store = _store(config)
    store.init()
    report = evaluate(config, store)
    for entry in report:
        print(json.dumps({**asdict(entry)}, ensure_ascii=False))
    sent = alert(config, store, report)
    if sent:
        print(json.dumps({"alerted": sent}))
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
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or verify the database").set_defaults(fn=cmd_init)
    sub.add_parser(
        "check-config", help="validate the config and report missing env vars"
    ).set_defaults(fn=cmd_check_config)
    sub.add_parser("stats", help="item counts per source").set_defaults(fn=cmd_stats)

    p = sub.add_parser("search", help="search from the terminal (admin: sees every source)")
    p.add_argument("query")
    p.add_argument("--source")
    p.add_argument("--project")
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("import-jsonl", help="import pre-summarised items from a JSONL file")
    p.add_argument("path")
    p.add_argument("--source", required=True)
    p.add_argument("--project")
    p.add_argument("--trust", choices=["own", "external"], default="own")
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

    sub.add_parser(
        "health", help="report stale or failing connectors (exit 1 if any); alert if set"
    ).set_defaults(fn=cmd_health)

    sub.add_parser("serve", help="run the MCP server").set_defaults(fn=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
