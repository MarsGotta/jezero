"""Command line interface: `jezero <command>`."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import tomllib
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
from .models import Item, clean_display
from .redact import Redaction, Redactor
from .store import Store, StoreError, sqlite_version

EXIT_LOCKED = 3
"""`retag` and `resummarize` exit with this when another run holds the lock: nothing was
done, and a script must not take that for success. (`run` skips quietly with 0, so a timer
firing during a long backfill is not an error.)"""


def _locked(command: str) -> int:
    print(
        f"error: another run is in progress; {command} did nothing, try again when it ends",
        file=sys.stderr,
    )
    return EXIT_LOCKED


def format_validation_error(exc: ValidationError) -> list[str]:
    """One line per problem: the field path and pydantic's message. No documentation URL
    and no input values (a wrong value may be a secret pasted in the wrong place)."""
    lines = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        path = ""
        for part in error.get("loc", ()):
            path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
        message = " ".join(str(error.get("msg", "invalid value")).split())
        lines.append(f"{path}: {message}" if path else message)
    return lines or ["invalid value"]


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
        # Stored text may hold terminal escape sequences; they are never printed.
        line = (
            f"[{item.id}] {hit.score:7.3f}  {item.occurred_at:%Y-%m-%d}  {item.source}  "
            f"{item.project or '-'}  {item.title}"
        )
        print(" ".join(clean_display(line).split("\n")))
    if not hits:
        print("no results")
    return 0


def cmd_eval(config: Config, args: argparse.Namespace) -> int:
    """Run a question file against the library and print how many cases pass."""
    from .evaluate import EvalFileError, compare, format_report, load_cases, run_cases, save

    try:
        cases, defaults = load_cases(Path(args.file))
    except (OSError, EvalFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sources = defaults.get("sources")
    if args.client:
        client = next((c for c in config.clients if c.name == args.client), None)
        if client is None:
            print(f"error: no client named {args.client!r} in [[clients]]", file=sys.stderr)
            return 2
        sources = list(client.sources)
    elif args.sources:
        sources = args.sources
    k = args.k or int(defaults.get("k", 5))
    report = run_cases(_store(config), config, cases, k=k, sources=sources)
    previous = None
    if args.compare:
        try:
            previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read {args.compare}: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        print(format_report(report, cases))
        if previous is not None:
            better, worse = compare(report, previous)
            was = f"{previous.get('passed')}/{previous.get('cases')}"
            print(
                f"before: {was}; better: {', '.join(better) or '-'};"
                f" worse: {', '.join(worse) or '-'}"
            )
    if args.save:
        save(report, Path(args.save))
    fail_under = args.fail_under if args.fail_under is not None else defaults.get("fail_under")
    if fail_under is not None and report.hit_at_k < float(fail_under):
        print(
            f"below the target: hit@{k} {report.hit_at_k:.0%} < {float(fail_under):.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


class _CleanRedactor(Redactor):
    """Drops terminal control characters (see `clean_display`) before redacting, so an
    imported title, summary, atom or original cannot carry escape sequences."""

    def redact(self, text: str) -> Redaction:
        return super().redact(clean_display(text) if text else text)


def cmd_import_jsonl(config: Config, args: argparse.Namespace) -> int:
    redactor = _CleanRedactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)
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

    from .pipeline import run_connector, scrub_error

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
            error = scrub_error(f"{type(exc).__name__}: {exc}", config)[:1000]
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
            return _locked("retag")
        batch = build_connector(config, conn).collect(ConnectorState(name=conn.name))
        rules = connector_rules(config, conn)
        changed = 0
        hinted = {doc.external_id for doc in batch.docs if doc.project_hint}
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
        asked = moved = 0
        if getattr(args, "ask_model", False):
            asked, moved = _retag_with_model(config, args, store, conn.name, hinted, rules)
    report = {"connector": conn.name, "docs": len(batch.docs), "retagged": changed}
    if getattr(args, "ask_model", False):
        report |= {"asked_model": asked, "moved_by_model": moved}
    print(json.dumps(report | {"dry_run": bool(args.dry_run)}))
    return 0


def _mentions(project: str, item: Item) -> bool:
    """Whether an item's own text names its project (`billing-api` also as "billing api")."""
    import re

    text = " ".join([item.title, item.summary, *item.questions, *(a.text for a in item.atoms)])
    pattern = r"\b" + re.escape(project).replace(r"\-", "[- ]?") + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _retag_with_model(config, args, store, source, hinted, rules) -> tuple[int, int]:
    """Ask the summarizer for the project of items with no source hint whose tag is not
    named in their own text. Returns (asked, changed); prints old -> new per item."""
    import asyncio

    from .summarize import Summarizer

    items = [
        i
        for i in store.timeline(source=source, limit=1_000_000)
        if i.external_id not in hinted and i.project and not _mentions(i.project, i)
    ]
    if not items:
        return 0, 0
    summarizer = Summarizer(config.summarizer, known_projects=config.summarizer.known_projects)

    async def ask(item):
        text = "\n".join([item.title, item.summary, *(f"{a.kind}: {a.text}" for a in item.atoms)])
        try:
            return item, rules.rename(await summarizer.guess_project(text))
        except Exception as exc:  # one failed call skips one item, never the rest
            print(f"[{item.id}] skipped: {exc}", file=sys.stderr)
            return item, item.project

    answers = asyncio.run(_gather(ask(i) for i in items))
    changes = []
    unsupported = 0
    for item, new in answers:
        if not new or new == item.project:
            continue
        # An answer the item's own text never names is no better supported than the old
        # tag: models still drift to listed slugs. Keep the old tag then.
        if not _mentions(new, item):
            unsupported += 1
            continue
        changes.append({"id": item.id, "old": item.project, "new": new})
        print(f"[{item.id}] {item.project} -> {new}  {item.title[:70]}", file=sys.stderr)
        if not args.dry_run:
            store.set_project(source, item.external_id, new)
    if args.log and changes:
        with Path(args.log).open("a", encoding="utf-8") as log:
            log.writelines(json.dumps(c) + "\n" for c in changes)
    moved = len(changes)
    usage = summarizer.usage
    print(
        f"model: {usage.calls} calls, {usage.input_tokens} input and "
        f"{usage.output_tokens} output tokens; {unsupported} answers not named in the "
        "item's text were left out",
        file=sys.stderr,
    )
    return len(items), moved


async def _gather(coros):
    import asyncio

    return await asyncio.gather(*coros)


def cmd_resummarize(config: Config, args: argparse.Namespace) -> int:
    """Summarise stored items again from their original text (ids from a file or a list)."""
    import asyncio
    import fcntl

    from .pipeline import resummarize_items

    raw = args.ids.split(",") if args.ids else []
    if args.ids_file:
        raw += Path(args.ids_file).read_text().split()
    words = [x.strip() for x in raw if x.strip()]
    wrong = [x for x in words if not (x.isascii() and x.isdigit())]
    if wrong:
        print(f"error: not an item id: {', '.join(wrong[:5])}", file=sys.stderr)
        return 2
    ids = sorted({int(x) for x in words})
    if not ids:
        print("error: no item ids given", file=sys.stderr)
        return 2
    store = _store(config)
    with config.db_path.with_suffix(".run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _locked("resummarize")
        report = asyncio.run(resummarize_items(config, store, ids))
    print(json.dumps(report.to_dict(), ensure_ascii=False))
    return 1 if report.failed else 0


def cmd_redact_stored(config: Config, args: argparse.Namespace) -> int:
    """Re-apply the current redaction rules to items already stored, and drop terminal
    control characters from what is shown of them (no LLM calls)."""
    redactor = Redactor(config.redaction.extra_patterns, enabled=config.redaction.enabled)

    def redact(text: str) -> tuple[str, int]:
        result = redactor.redact(text)
        return result.text, result.count

    store = _store(config)
    store.init()
    items, total = store.redact_stored(redact, dry_run=args.dry_run)
    verb = "would redact" if args.dry_run else "redacted"
    print(f"{verb} {total} secrets in {items} items")
    cleaned = clean_stored_controls(store, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} terminal control characters from {cleaned} items")
    return 0


def clean_stored_controls(store: Store, *, dry_run: bool) -> int:
    """Drop terminal control characters (`clean_display`) from the title, summary,
    questions and atoms of stored items, and keep the search index in sync. Returns how
    many items had some.

    The kept original and `content_hash` are left alone: the hash is that of the redacted
    source text, which is what the next run compares, so no item is summarised again. The
    original is cleaned when it is shown instead.
    """
    con = store.connect()
    changed = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT id, title, summary, questions, atoms, project FROM items"
        ).fetchall()
        for row in rows:
            dirty = False

            def clean(text: str) -> str:
                nonlocal dirty
                new = clean_display(text)
                dirty = dirty or new != text
                return new

            title, summary = clean(row["title"]), clean(row["summary"])
            questions = [
                clean(q) if isinstance(q, str) else q for q in json.loads(row["questions"] or "[]")
            ]
            atoms = json.loads(row["atoms"] or "[]")
            for atom in atoms:
                if isinstance(atom, dict) and isinstance(atom.get("text"), str):
                    atom["text"] = clean(atom["text"])
            if not dirty:
                continue
            changed += 1
            new_questions = json.dumps(questions, ensure_ascii=False)
            new_atoms = json.dumps(atoms, ensure_ascii=False)
            if dry_run:
                continue
            con.execute(
                "UPDATE items SET title = ?, summary = ?, questions = ?, atoms = ? WHERE id = ?",
                (title, summary, new_questions, new_atoms, row["id"]),
            )
            con.execute("DELETE FROM items_fts WHERE rowid = ?", (row["id"],))
            con.execute(
                "INSERT INTO items_fts (rowid, title, summary, questions, atoms, project) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    title,
                    summary,
                    "\n".join(str(q or "") for q in questions),
                    "\n".join(
                        str(a.get("text") or "") if isinstance(a, dict) else str(a) for a in atoms
                    ),
                    row["project"] or "",
                ),
            )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return changed


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
            (clean_display(str(e["last_error"]))[:60] if e["last_error"] else "-"),
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

    p = sub.add_parser("eval", help="measure search quality with a file of questions and answers")
    p.add_argument("file", help="question file (TOML, see examples/eval.example.toml)")
    p.add_argument(
        "--k", type=int, help="a case passes if an expected item is in the top K (default 5)"
    )
    who = p.add_mutually_exclusive_group()
    who.add_argument("--client", help="read with the sources of this [[clients]] entry")
    who.add_argument("--sources", nargs="+", metavar="GLOB", help="read only these sources")
    p.add_argument("--save", metavar="PATH", help="write the result as JSON, to compare later")
    p.add_argument("--compare", metavar="PATH", help="a result saved earlier: show what changed")
    p.add_argument(
        "--fail-under", type=float, metavar="SHARE", help="exit 1 if hit@K is below this (0 to 1)"
    )
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(fn=cmd_eval)

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
    p.add_argument(
        "--limit",
        type=int,
        help="process about N units, oldest first; it may take more than N to keep units "
        "that share a timestamp in the same run",
    )
    p.add_argument("--sample", type=int, help="process the newest N units, cursor untouched")
    p.add_argument("--dry-run", action="store_true", help="count work without calling the LLM")
    p.add_argument(
        "--due", action="store_true", help="only connectors whose interval_minutes has elapsed"
    )
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser(
        "redact-stored",
        help="apply the current redaction rules to stored items and drop terminal control "
        "characters from their titles, summaries, questions and atoms (no LLM calls)",
    )
    p.add_argument("--dry-run", action="store_true", help="count changes without writing")
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

    p = sub.add_parser(
        "resummarize",
        help="summarise stored items again from their original (exit 3 if a run holds the lock)",
    )
    p.add_argument("--ids", help="comma-separated item ids")
    p.add_argument("--ids-file", help="file with item ids, one per line")
    p.set_defaults(fn=cmd_resummarize)

    p = sub.add_parser(
        "retag",
        help="re-apply the source's project hints and the project aliases, without the LLM"
        " (exit 3 if a run holds the lock)",
    )
    p.add_argument("--connector", required=True, help="connector whose items are retagged")
    p.add_argument("--dry-run", action="store_true", help="count changes without writing")
    p.add_argument(
        "--ask-model",
        action="store_true",
        help="also ask the summarizer for the project of items whose source gives none and "
        "whose tag their own text never mentions (one short call each, from the summary)",
    )
    p.add_argument("--log", metavar="PATH", help="write every change (id, old, new) as JSON lines")
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
        for line in format_validation_error(exc):
            print(f"invalid config: {line}", file=sys.stderr)
        return 2
    except tomllib.TOMLDecodeError as exc:
        print(f"error: the config is not valid TOML: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
