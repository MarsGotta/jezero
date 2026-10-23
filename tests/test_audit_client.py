"""Regression tests for the client 0.5.2 audit fixes: redirects and plain HTTP, a queue
that never stops on one bad file, large files, symlinks and files outside an agent's
store, Codex config.toml edits, private files, concurrent state writers, the OpenCode
installer, the stdio bridge, odd hook input, and the Antigravity plugin's paths."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import tomllib
import urllib.error
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .test_client import CORE, DEVICE_TOKEN, configure, load_core
from .test_client_fixes import child_env, write_key

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------ stubs


class Stub(BaseHTTPRequestHandler):
    """A server whose answer comes from `respond(handler, body) -> (status, headers,
    body)`. When `reset(handler)` is true it drops the connection instead, before
    reading the body (what a server refusing a huge upload early looks like). It
    records every request in `seen`."""

    respond: Callable[[Any, bytes], Any] = staticmethod(lambda h, b: (200, {}, b"{}"))
    reset: Callable[[Any], bool] = staticmethod(lambda h: False)
    seen: list[dict[str, Any]]  # set per server

    def _handle(self) -> None:
        if type(self).reset(self):
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_connection = True
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        status, headers, payload = type(self).respond(self, body)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_PUT = do_POST = _handle  # noqa: N815

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@pytest.fixture
def make_stub() -> Iterator[Callable[..., tuple[str, type[Stub]]]]:
    servers: list[ThreadingHTTPServer] = []

    def make(
        respond: Callable[[Any, bytes], Any] | None = None,
        reset: Callable[[Any], bool] | None = None,
    ) -> tuple[str, type[Stub]]:
        attrs: dict[str, Any] = {"seen": []}
        if respond is not None:
            attrs["respond"] = staticmethod(respond)
        if reset is not None:
            attrs["reset"] = staticmethod(reset)
        handler = type("AuditStub", (Stub,), attrs)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", handler

    yield make
    for httpd in servers:
        httpd.shutdown()


def claude_transcript(core: ModuleType, name: str, text: str = '{"type":"user"}\n') -> Path:
    """A transcript inside Claude Code's store (`$CLAUDE_CONFIG_DIR/projects`)."""
    path = core._claude_dir() / "projects" / "-w" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def pending(core: ModuleType) -> list[Path]:
    return sorted((core.SPOOL / "pending").glob("*.json"))


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_version_is_bumped(tmp_path, monkeypatch) -> None:
    assert load_core(tmp_path / "jz", monkeypatch).VERSION == "0.5.3"


# ------------------------------------------------- 1. redirects and plain HTTP


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed(make_stub, tmp_path, monkeypatch, code) -> None:
    other, other_handler = make_stub()  # "another host": must never see the key
    location = other + "/stolen"
    url, handler = make_stub(lambda h, b: (code, {"Location": location}, b""))
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    with pytest.raises(core.RedirectRefused) as caught:
        core._http("GET", url + "/inbox/v1/ping", DEVICE_TOKEN)
    assert location in str(caught.value) and str(code) in str(caught.value)
    assert other_handler.seen == []  # nothing reached the redirect's target
    # A POST through the bridge gets a clear JSON-RPC error, not silence.
    out = io.StringIO()
    line = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    core.mcp_proxy(io.StringIO(line + "\n"), out)
    (reply,) = [json.loads(x) for x in out.getvalue().splitlines()]
    assert reply["id"] == 4 and location in reply["error"]["message"]
    assert other_handler.seen == []
    # An upload stops the queue (the address is wrong) and never sends the key elsewhere.
    claude = claude_transcript(core, "r")
    assert core.enqueue("claude-code", "r", str(claude), "/w")
    assert core.flush(core.load_config())["failed"] == 1 and len(pending(core)) == 1
    assert other_handler.seen == [] and "redirect" in core.LOG.read_text()


def test_authorization_is_not_copied_to_redirects(tmp_path, monkeypatch) -> None:
    """Belt and braces: the key is an unredirected header, so even a handler that did
    follow a redirect would not copy it."""
    core = load_core(tmp_path / "jz", monkeypatch)
    sent: list[Any] = []

    def fake_open(request, timeout, context):  # noqa: ANN001
        sent.append(request)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(core, "_open", fake_open)
    with pytest.raises(urllib.error.URLError):
        core._http("GET", "https://lib.example/x", "secret")
    assert sent[0].unredirected_hdrs["Authorization"] == "Bearer secret"
    assert "Authorization" not in sent[0].headers


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://lib.example", True),
        ("http://127.0.0.1:8765", True),
        ("http://localhost:8765/mcp", True),
        ("http://[::1]:8765", True),
        ("http://lib.example", False),
        ("http://192.168.1.10:8765", False),  # privacy: allow
        ("ftp://lib.example", False),
        ("lib.example", False),
    ],
)
def test_init_refuses_plain_http_to_another_computer(tmp_path, monkeypatch, capsys, url, ok):
    core = load_core(tmp_path / "jz", monkeypatch)
    code = core.init(url, "laptop", force=False)
    assert (code == 0) is ok
    if not ok:
        assert code == 2 and not core.TOKEN_FILE.exists()
        assert "url:" in capsys.readouterr().err


def test_insecure_http_flag(tmp_path, monkeypatch, capsys) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    args = ["init", "--url", "http://jezero.vpn:8765", "--device", "laptop"]
    assert core.main(args) == 2
    assert "--insecure-http" in capsys.readouterr().err
    assert core.main([*args, "--insecure-http"]) == 0
    assert json.loads(core.CONFIG.read_text())["insecure_http"] is True
    # A plain-HTTP address written by hand is reported by status.
    cfg = json.loads(core.CONFIG.read_text())
    cfg.pop("insecure_http")
    core.CONFIG.write_text(json.dumps(cfg))
    monkeypatch.setattr(core, "_http", lambda *a, **k: (200, b"{}"))
    core.status()
    assert "warning:" in capsys.readouterr().out


# ----------------------------------------- 2. one bad file never stops the queue


def test_project_header_ascii_as_is_unicode_encoded(make_stub, tmp_path, monkeypatch) -> None:
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    for name, project in (("a", "proyecto-ñandú"), ("b", "billing"), ("c", "x y")):
        assert core.enqueue("claude-code", name, str(claude_transcript(core, name)), None)
        pointer = core.SPOOL / "pending" / f"claude-code__{name}.json"
        pointer.write_text(json.dumps({**json.loads(pointer.read_text()), "project": project}))
    assert core.flush(core.load_config())["sent"] == 3
    projects = [r["headers"].get("X-Jezero-Project") for r in handler.seen]
    # 0.5.3: a name with letters beyond ASCII goes percent-encoded (UTF-8, NFC).
    assert projects == ["proyecto-%C3%B1and%C3%BA", "billing", None]
    assert "not sent" in core.LOG.read_text()


def test_codex_names_with_spaces_and_hash(make_stub, tmp_path, monkeypatch) -> None:
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    day = codex / "sessions" / "2026" / "10" / "21"
    day.mkdir(parents=True)
    name = "rollout-2026-10-21T10-00-00 a#b.jsonl"
    (day / name).write_text("{}\n")
    rel = core.relpath_for("codex", day / name)
    assert rel == "sessions/2026/10/21/rollout-2026-10-21T10-00-00_a_b.jsonl"
    assert core.enqueue("codex", "s", str(day / name), "/w")
    assert core.flush(core.load_config())["sent"] == 1
    assert handler.seen[0]["path"] == "/inbox/v1/codex/" + rel
    assert core.inbox_url({"url": "https://h/"}, "codex", "sessions/x y#z?.jsonl") == (
        "https://h/inbox/v1/codex/sessions/x%20y%23z%3F.jsonl"
    )


def test_unexpected_error_rejects_one_file_and_goes_on(make_stub, tmp_path, monkeypatch):
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    first, second = claude_transcript(core, "a"), claude_transcript(core, "b")
    core.enqueue("claude-code", "a", str(first), "/w")
    core.enqueue("claude-code", "b", str(second), "/w")
    real = core.upload

    def flaky(cfg, agent, path, project=None, roots=None, sent=None):  # noqa: ANN001
        if path == first:
            raise UnicodeEncodeError("latin-1", "ñ", 0, 1, "ordinal not in range")
        return real(cfg, agent, path, project, roots, sent)

    monkeypatch.setattr(core, "upload", flaky)
    counts = core.flush(core.load_config())
    assert counts["rejected"] == 1 and counts["sent"] == 1 and not pending(core)
    reason = json.loads(core.STATE.read_text())["rejected"][str(first)]["reason"]
    assert "UnicodeEncodeError" in reason


# ---------------------------------------------------------------- 3. large files


def test_file_over_the_limit_is_rejected_without_reading(make_stub, tmp_path, monkeypatch):
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    cfg = json.loads(core.CONFIG.read_text())
    core.CONFIG.write_text(json.dumps({**cfg, "max_upload_mb": 0.01}))
    big = claude_transcript(core, "big", "x" * 20_000)
    core.enqueue("claude-code", "big", str(big), "/w")
    monkeypatch.setattr(core.Path, "read_bytes", lambda self: pytest.fail("read whole file"))
    assert core.flush(core.load_config())["rejected"] == 1
    assert handler.seen == []  # never sent
    assert (
        "over the upload limit"
        in json.loads(core.STATE.read_text())["rejected"][str(big)]["reason"]
    )


def test_upload_streams_through_a_private_temp_file(make_stub, tmp_path, monkeypatch) -> None:
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    path = claude_transcript(core, "s", "line\n" * 50_000)
    monkeypatch.setattr(core.gzip, "compress", lambda *a, **k: pytest.fail("in memory"))
    core.enqueue("claude-code", "s", str(path), "/w")
    assert core.flush(core.load_config())["sent"] == 1
    import gzip

    assert gzip.decompress(handler.seen[0]["body"]) == path.read_bytes()
    assert int(handler.seen[0]["headers"]["Content-Length"]) == len(handler.seen[0]["body"])
    assert not [p for p in core.SPOOL.iterdir() if p.name.endswith(".tmp")]


def test_resets_on_a_large_upload_end_in_rejected(make_stub, tmp_path, monkeypatch) -> None:
    url, handler = make_stub(reset=lambda h: int(h.headers.get("Content-Length") or 0) > 1 << 20)
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    big = claude_transcript(core, "a-big")
    big.write_bytes(os.urandom(2 << 20))  # incompressible
    small = claude_transcript(core, "b-small")
    core.enqueue("claude-code", "a-big", str(big), "/w")
    core.enqueue("claude-code", "b-small", str(small), "/w")
    for attempt in (1, 2):
        counts = core.flush(core.load_config())
        assert counts["failed"] == 1 and counts["sent"] == 0, counts
        assert json.loads(core.STATE.read_text())["attempts"][str(big)]["count"] == attempt
    counts = core.flush(core.load_config())
    assert counts["rejected"] == 1 and counts["sent"] == 1 and not pending(core)
    state = json.loads(core.STATE.read_text())
    assert "connection reset 3 times" in state["rejected"][str(big)]["reason"]
    assert str(big) not in state.get("attempts", {})


def test_server_down_is_not_a_reset(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    assert not core._is_reset(urllib.error.URLError(ConnectionRefusedError()))
    assert core._is_reset(urllib.error.URLError(ConnectionResetError()))
    assert core._is_reset(BrokenPipeError())


# ------------------------------------------- 4. symlinks and files outside the store


def test_symlinks_and_outside_files_are_never_uploaded(make_stub, tmp_path, monkeypatch):
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    secret = tmp_path / "ssh" / "id_ed25519"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY")
    projects = core._claude_dir() / "projects"
    (projects / "-w").mkdir(parents=True)
    evil = projects / "-w" / "evil.jsonl"
    evil.symlink_to(secret)
    assert not core.enqueue("claude-code", "evil", str(evil), "/w")
    assert "symbolic link" in core.LOG.read_text()
    # A hook naming any .jsonl outside the agent's store.
    outside = tmp_path / "elsewhere" / "-w" / "notes.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}\n")
    core.hook("session-end", "claude-code", {"session_id": "x", "transcript_path": str(outside)})
    # A symlinked folder inside the store that leads out of it.
    (projects / "-linked").symlink_to(outside.parent)
    assert not core.enqueue("claude-code", "l", str(projects / "-linked" / "notes.jsonl"), "/w")
    # Backfill skips them too.
    core.backfill(["claude-code"], days=1)
    assert pending(core) == []
    # A file swapped for a symlink after it was queued is caught at upload time.
    real = claude_transcript(core, "real")
    assert core.enqueue("claude-code", "real", str(real), "/w")
    real.unlink()
    real.symlink_to(secret)
    # 0.5.3: never sent, and not the server's verdict: it waits in the queue instead of
    # being marked rejected for good.
    counts = core.flush(core.load_config())
    assert counts["waiting"] == 1 and counts["rejected"] == 0
    assert handler.seen == []
    assert str(real) not in json.loads(core.STATE.read_text()).get("rejected", {})


def test_store_reached_through_a_symlink_still_works(make_stub, tmp_path, monkeypatch):
    """Dotfile setups often link ~/.claude elsewhere: only the file itself may not be a
    link, and its real place must be inside the store's real place."""
    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    real_dir = tmp_path / "dotfiles" / "claude"
    (real_dir / "projects" / "-w").mkdir(parents=True)
    link = tmp_path / "linked-claude"
    link.symlink_to(real_dir)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
    transcript = link / "projects" / "-w" / "s.jsonl"
    transcript.write_text("{}\n")
    assert core.enqueue("claude-code", "s", str(transcript), "/w")
    assert core.flush(core.load_config())["sent"] == 1


def test_antigravity_prompt_is_only_read_from_its_store(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    line = json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>hi there</USER_REQUEST>"})
    outside = tmp_path / "x" / "brain" / "c0ffee00" / "logs" / "transcript.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text(line + "\n")
    assert core.antigravity_prompt(str(outside)) == ""
    inside = tmp_path / "user" / ".gemini" / "antigravity" / "brain" / "c0ffee00" / "t.jsonl"
    inside.parent.mkdir(parents=True)
    inside.write_text(line + "\n")
    assert core.antigravity_prompt(str(inside)) == "hi there"


# ------------------------------------------------------------ 5. Codex config.toml

HTTP_ENTRY = '[mcp_servers.jezero]\nurl = "https://lib.example/mcp"\nbearer_token = "OLDKEY"\n'


@pytest.fixture(params=["tomllib", "scanner only"])
def codex(request, tmp_path, monkeypatch) -> tuple[ModuleType, Path]:
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "https://lib.example")
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    if request.param == "scanner only":  # Python 3.9 and 3.10 have no tomllib
        monkeypatch.setattr(core, "_toml_parse", lambda text: None)
    return core, home / "config.toml"


def backups(config: Path) -> list[Path]:
    return sorted(config.parent.glob("config.toml.bak-*"))


@pytest.mark.parametrize(
    "text",
    [
        '[mcp_servers]\njezero = { url = "https://lib.example/mcp", bearer_token = "OLDKEY" }\n',
        'mcp_servers.jezero.url = "https://lib.example/mcp"\nmodel = "x"\n',
        '[mcp_servers]\njezero.url = "https://lib.example/mcp"\n',
        'mcp_servers = { jezero = { url = "https://lib.example/mcp" } }\n',
        HTTP_ENTRY + '[other]\nx = 1\n[mcp_servers.jezero.env]\nA = "b"\n',
    ],
)
def test_codex_forms_it_cannot_edit_are_refused(codex, text) -> None:
    core, config = codex
    config.write_bytes(text.encode())
    out = core._setup_codex()
    assert "cannot edit safely" in out and "Remove every mcp_servers.jezero" in out
    assert config.read_bytes() == text.encode() and backups(config) == []


@pytest.mark.parametrize(
    "header", ['[mcp_servers."jezero"]', "[ mcp_servers . 'jezero' ]", "[mcp_servers.jezero]"]
)
def test_codex_old_entry_is_replaced_whole(codex, header) -> None:
    core, config = codex
    before = 'model = "x"\n[mcp_servers.other]\ncommand = "o"\nargs = [\n  ["mcp"],\n  "b",\n]\n\n'
    old = (
        f'{header}\nurl = "https://lib.example/mcp"\nargs = [\n  ["mcp"],\n]\n'
        'bearer_token = "OLDKEY"\n[mcp_servers.jezero.http_headers]\n'
        'Authorization = "Bearer OLDKEY"\n\n'
    )
    after = '[profiles.fast]\nmodel = "y"\n'
    config.write_text(before + old + after)
    out = core._setup_codex()
    assert "replaced the old jezero entry" in out and "rotate" in out, out
    text = config.read_text()
    assert "OLDKEY" not in text and text.startswith(before) and text.endswith(after)
    data = tomllib.loads(text)
    assert set(data["mcp_servers"]) == {"other", "jezero"}
    assert data["mcp_servers"]["jezero"]["command"] == "python3"
    assert data["mcp_servers"]["other"]["args"] == [["mcp"], "b"]
    (backup,) = backups(config)
    assert backup.read_text() == before + old + after and mode(backup) == 0o600
    assert core._setup_codex() == "codex: MCP server jezero already in config.toml"


def test_codex_keeps_crlf_and_the_file_mode(codex) -> None:
    core, config = codex
    before = 'model = "x"\r\n\r\n[mcp_servers.other]\r\ncommand = "o"\r\n'
    config.write_bytes(before.encode())
    config.chmod(0o640)
    assert "added to config.toml" in core._setup_codex()
    raw = config.read_bytes()
    assert raw.startswith(before.encode()) and b"\n" not in raw.replace(b"\r\n", b"")
    assert tomllib.loads(raw.decode())["mcp_servers"]["jezero"]["command"] == "python3"
    assert mode(config) == 0o640
    assert not [p for p in config.parent.iterdir() if p.name.endswith(".tmp")]
    # Replacing an old entry in a CRLF file keeps CRLF too.
    config.write_bytes((before + HTTP_ENTRY.replace("\n", "\r\n")).encode())
    assert "replaced" in core._setup_codex()
    raw = config.read_bytes()
    assert b"OLDKEY" not in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_codex_invalid_toml_is_left_alone(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "https://lib.example")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text("model = \n")
    assert "not valid TOML" in core._setup_codex()
    assert config.read_text() == "model = \n"


# --------------------------------------------------------- 6. private files


def test_client_files_are_private_and_exports_go_away(make_stub, tmp_path, monkeypatch):
    url, handler = make_stub()
    home = tmp_path / "jz"
    core = load_core(home, monkeypatch)
    assert core.init("http://127.0.0.1:9", "laptop", force=False) == 0
    configure(core, url)
    core.CONFIG.write_text(json.dumps({"url": url, "device": "laptop"}))
    assert mode(home) == 0o700
    exports = core.SPOOL / "opencode"
    exports.mkdir(parents=True)
    export = exports / "ses_1.json"
    export.write_text('{"session": {}}')
    assert core.enqueue("opencode", "ses_1", str(export), "/w")
    core.log("hello")
    core.update_state(lambda s: s.__setitem__("x", 1))
    for path in (core.STATE, core.LOG, core.TOKEN_FILE, *pending(core)):
        assert mode(path) == 0o600, path
    for path in (core.SPOOL, core.SPOOL / "pending"):
        assert mode(path) == 0o700, path
    assert core.flush(core.load_config())["sent"] == 1
    assert not export.exists()  # uploaded: no copy left behind
    core._save_json(core.CONFIG, {"url": url, "device": "laptop"})
    assert mode(core.CONFIG) == 0o600
    # The same session exported again is not uploaded again.
    export.write_text('{"session": {}}')
    assert core.enqueue("opencode", "ses_1", str(export), "/w")
    assert core.flush(core.load_config())["unchanged"] == 1 and len(handler.seen) == 1
    assert not export.exists()


def test_hermes_export_is_private_and_not_sent_twice(make_stub, tmp_path, monkeypatch):
    from .test_hermes_plugin import make_state_db

    url, handler = make_stub()
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    db = make_state_db(tmp_path / "hermes")
    path = core.export_hermes(db, "h1")
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert core.enqueue("hermes", "h1", str(path), None)
    assert core.flush(core.load_config())["sent"] == 1 and not path.exists()
    assert core.export_hermes(db, "h1") is None  # unchanged since it was uploaded


def test_opencode_plugin_writes_private_exports() -> None:
    ts = (ROOT / "clients" / "opencode" / "jezero.ts").read_text()
    assert "mode: 0o700" in ts and "mode: 0o600" in ts and "chmodSync(file, 0o600)" in ts
    assert '${id.replace(/[^A-Za-z0-9._-]/g, "_")}.json' in ts  # no path from the event


# ---------------------------------------------------------- 7. concurrent state


def test_concurrent_state_writers_lose_nothing(tmp_path, monkeypatch) -> None:
    home = tmp_path / "jz"
    core = load_core(home, monkeypatch)

    def add(prefix: str) -> None:
        for i in range(40):
            core.update_state(lambda s, k=f"{prefix}{i}": s.setdefault("t", {}).__setitem__(k, 1))

    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('c', {str(CORE)!r})\n"
        "c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)\n"
        "for i in range(40):\n"
        "    key = f'{sys.argv[1]}{i}'\n"
        "    c.update_state(lambda s, k=key: s.setdefault('t', {}).__setitem__(k, 1))\n"
    )
    env = child_env(JEZERO_CLIENT_HOME=str(home), PYTHONPATH=str(ROOT / "src"))
    procs = [subprocess.Popen([sys.executable, "-c", script, f"p{n}-"], env=env) for n in range(3)]
    threads = [threading.Thread(target=add, args=(f"t{n}-",)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for p in procs:
        assert p.wait(timeout=120) == 0
    assert len(json.loads(core.STATE.read_text())["t"]) == 6 * 40
    assert not [p for p in home.iterdir() if p.name.endswith(".tmp")]


# ----------------------------------------------------- 8. OpenCode installer path


def test_opencode_installer_writes_a_valid_string(tmp_path, monkeypatch) -> None:
    home = tmp_path / 'we"ird\\home'
    core = load_core(home, monkeypatch)
    configure(core, "https://lib.example")
    xdg = tmp_path / "xdg"
    (xdg / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    source = ROOT / "clients" / "opencode" / "jezero.ts"
    assert "installed in" in core._setup_opencode(source)
    text = (xdg / "opencode" / "plugins" / "jezero.ts").read_text()
    literal = text.split("const INSTALLED_CORE = ", 1)[1].splitlines()[0]
    assert json.loads(literal) == str(core.HOME / "core" / "jezero_client.py")
    node = shutil.which("node")
    if node:  # the literal is valid JavaScript, and evaluates to the same path
        run = subprocess.run(
            [node, "-e", f"process.stdout.write({literal})"], capture_output=True, text=True
        )
        assert run.stdout == str(core.HOME / "core" / "jezero_client.py")


# ------------------------------------------------------------ 9. the stdio bridge


def bridge(core: ModuleType, *lines: str) -> list[dict[str, Any]]:
    out = io.StringIO()
    core.mcp_proxy(io.StringIO("".join(line + "\n" for line in lines)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]  # every line is JSON


CALL = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})


@pytest.mark.parametrize(
    ("status", "body", "expect"),
    [
        (200, b"<html>proxy login</html>", "not JSON-RPC"),
        (200, b'{"hello": "world"}', "not JSON-RPC"),
        (200, b"", "no answer"),
        (400, b"bad request", "HTTP 400"),
        (401, b"no", "refused this computer's key"),
        (502, b"bad gateway", "not working right now (HTTP 502)"),
    ],
)
def test_bridge_answers_every_request(make_stub, tmp_path, monkeypatch, status, body, expect):
    url, _ = make_stub(lambda h, b: (status, {}, body))
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    (reply,) = bridge(core, CALL)
    assert reply["id"] == 9 and expect in reply["error"]["message"], reply
    assert "not reachable" not in reply["error"]["message"]


def test_bridge_forwards_valid_answers_and_errors(make_stub, tmp_path, monkeypatch) -> None:
    def respond(handler, body):  # noqa: ANN001
        message = json.loads(body)
        if "id" not in message:
            return 202, {}, b""
        if message["method"] == "bad":
            error = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "x"},
            }
            return 400, {"Content-Type": "application/json"}, json.dumps(error).encode()
        result = {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}
        return (
            200,
            {"Content-Type": "text/event-stream"},
            f"event: message\ndata: {json.dumps(result)}\n\n".encode(),
        )

    url, _ = make_stub(respond)
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, url)
    replies = bridge(
        core,
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        CALL,
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "bad"}),
    )
    assert replies == [
        {"jsonrpc": "2.0", "id": 9, "result": {"ok": True}},
        {"jsonrpc": "2.0", "id": 10, "error": {"code": -32601, "message": "x"}},
    ]


def test_bridge_parse_errors_and_batches(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "http://127.0.0.1:9")
    batch = json.dumps(
        [{"jsonrpc": "2.0", "id": 1, "method": "a"}, {"jsonrpc": "2.0", "id": 2, "method": "b"}]
    )
    replies = bridge(core, "not json", batch, "42")
    assert replies[0]["id"] is None and replies[0]["error"]["code"] == -32700
    assert [(r["id"], r["error"]["code"]) for r in replies[1:3]] == [(1, -32600), (2, -32600)]
    assert replies[3]["id"] is None and replies[3]["error"]["code"] == -32600
    (offline,) = bridge(core, CALL)  # nothing listens on port 9
    assert offline["id"] == 9 and "not reachable" in offline["error"]["message"]


# -------------------------------------------------------------- 10. odd hook input


@pytest.mark.parametrize("stdin", ["[1, 2]", "42", '"text"', "null", "{not json"])
@pytest.mark.parametrize("agent", ["claude-code", "codex", "antigravity"])
def test_hook_with_non_object_json(tmp_path, stdin, agent) -> None:
    home = tmp_path / "jz"
    write_key(home, "http://127.0.0.1:9")
    for event in ("session-start", "stop"):
        run = subprocess.run(
            [sys.executable, str(CORE), "hook", event, "--agent", agent],
            input=stdin,
            capture_output=True,
            text=True,
            env=child_env(JEZERO_CLIENT_HOME=str(home), HOME=str(tmp_path / "user")),
            timeout=30,
        )
        assert run.returncode == 0 and "Traceback" not in run.stderr, run.stderr
        if agent == "antigravity":
            json.loads(run.stdout)


# ------------------------------------------- setup notes: Claude's backups, Antigravity


def test_claude_setup_warns_about_the_old_key(tmp_path, monkeypatch) -> None:
    from .test_client_fixes import fake_cli

    core = load_core(tmp_path / "jz", monkeypatch)
    configure(core, "https://lib.example")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    fake_cli(tmp_path, "claude")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    old = {"type": "http", "url": "https://lib.example/mcp", "headers": {"Authorization": "x"}}
    (claude_dir / ".claude.json").write_text(json.dumps({"mcpServers": {"jezero": old}}))
    out = core._setup_claude()
    assert str(claude_dir / "backups") in out and "jezero device rotate" in out
    assert "init --force" in out


def test_antigravity_setup_points_the_plugin_at_the_full_path(tmp_path, monkeypatch) -> None:
    core = load_core(tmp_path / "jz", monkeypatch)
    user = tmp_path / "user"
    monkeypatch.setenv("HOME", str(user))
    write_key(core.HOME, "http://127.0.0.1:9")
    assert "agy plugin install" in core._setup_antigravity()  # not installed yet
    plugin = user / ".gemini" / "config" / "plugins" / "jezero"
    shutil.copytree(ROOT / "clients" / "antigravity", plugin)
    (plugin / "core").mkdir()
    shutil.copy2(CORE, plugin / "core" / "jezero_client.py")
    shutil.copy2(ROOT / "src" / "jezero" / "projects.py", plugin / "core" / "jezero_projects.py")
    out = core._setup_antigravity()
    assert "now runs the core by its full path" in out
    full = str(plugin / "core" / "jezero_client.py")
    hooks = json.loads((plugin / "hooks.json").read_text())["jezero"]
    for handlers in hooks.values():
        assert handlers[0]["command"].startswith(f'python3 "{full}" hook ')
    server = json.loads((plugin / "mcp_config.json").read_text())["mcpServers"]["jezero"]
    assert server["args"][0] == full
    assert "already runs the core by its full path" in core._setup_antigravity()
    # The hook now works from any folder.
    run = subprocess.run(  # noqa: S602 - the command line Antigravity itself runs
        hooks["SessionStart"][0]["command"],
        shell=True,
        input="{}",
        capture_output=True,
        text=True,
        cwd="/",
        env=child_env(JEZERO_CLIENT_HOME=str(core.HOME), HOME=str(user)),
        timeout=30,
    )
    assert run.returncode == 0 and json.loads(run.stdout) is not None, run.stderr
