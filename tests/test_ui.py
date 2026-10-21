"""The `jezero` app and the people commands, on demo data and on fake agent folders."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jezero import client_core as core
from jezero.cli import main
from jezero.ui.app import JezeroApp
from jezero.ui.data import DemoBackend, ago, detect_agents, last_seen, source_agent
from jezero.ui.look import PALETTES, sparkline, wordmark


def run(coro):  # noqa: ANN001, ANN201 - tiny helper
    return asyncio.run(coro)


def test_app_walks_every_view_on_demo_data() -> None:
    async def scenario() -> None:
        app = JezeroApp(backend=DemoBackend(), demo=True)
        async with app.run_test(size=(160, 46)) as pilot:
            await pilot.pause(0.5)
            assert app.overview_data["items"] > 0
            assert str(app.query_one("#t-items").query_one("Digits").value) == "1,681"
            assert len(app.latest_items) == 6

            await pilot.press("2")
            for key in ["r", "e", "t", "r", "y", "space", "b", "a", "c", "k", "o", "f", "f"]:
                await pilot.press(key)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.last_hits is not None and [h["id"] for h in app.last_hits[1]] == [2412, 2390]
            assert app.current is not None and app.current["id"] == 2412
            await pilot.press("l")
            await pilot.pause(0.4)
            assert app.current_newer == []

            await pilot.press("3")
            await pilot.pause(0.4)
            assert app.timeline_state is not None and len(app.timeline_state[1]) == 6

            await pilot.press("4")
            await pilot.pause(0.3)
            assert app.query_one("#agent-codex").has_class("-outdated")
            assert app.query_one("#agent-antigravity").has_class("-missing")

            await pilot.press("5")
            app.query_one("#ctx-project").value = "atlas"
            app.query_one("#ctx-prompt").value = "uploads stall retry backoff"
            app.refresh_context()
            await pilot.pause(0.4)
            first = str(app.query_one("#ctx-first-text").render())
            assert "2412" in first

            await pilot.press("escape", "t")
            await pilot.pause(0.2)
            assert app.theme == "jezero-dawn"

    run(scenario())


def test_app_without_a_key_explains_how_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "load_config", lambda: None)

    async def scenario() -> None:
        app = JezeroApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            assert app.problem and "jezero connect" in app.problem
            assert "Not connected" in str(app.query_one("#bars").render())

    run(scenario())


def test_detect_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEZERO_CLIENT_HOME", str(tmp_path / "jz"))

    def core_file(folder: Path, version: str) -> None:
        folder.mkdir(parents=True)
        (folder / "jezero_client.py").write_text(f'VERSION = "{version}"\n')

    core_file(tmp_path / ".claude/plugins/cache/jezero/jezero/0.3.2/core", "0.3.2")
    core_file(tmp_path / ".claude/plugins/cache/jezero/jezero/0.4.0/core", core.VERSION)
    core_file(tmp_path / ".codex/plugins/cache/jezero/jezero/0.3.1/core", "0.3.1")
    (tmp_path / ".codex/config.toml").write_text("[mcp_servers.jezero]\nurl = 'x'\n")
    core_file(tmp_path / ".hermes/profiles/main/plugins/jezero/core", core.VERSION)
    (tmp_path / ".config/opencode/plugins").mkdir(parents=True)
    (tmp_path / ".config/opencode/plugins/jezero.ts").write_text("// plugin")
    core_file(tmp_path / "jz/core", core.VERSION)

    found = {s.agent: s for s in detect_agents(tmp_path)}
    assert found["claude-code"].version == core.VERSION and not found["claude-code"].outdated
    assert found["claude-code"].where and found["claude-code"].where.endswith(core.VERSION)
    assert found["codex"].outdated and "MCP server registered" in found["codex"].notes
    assert found["opencode"].installed and found["opencode"].version == core.VERSION
    assert found["hermes"].installed and found["hermes"].notes == ["profiles: main"]
    assert not found["antigravity"].installed
    assert found["antigravity"].install_command[:3] == ["agy", "plugin", "install"]
    assert found["claude-code"].install_command[-2:] == ["--agent", "claude-code"]


def test_helpers() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert ago((now - timedelta(minutes=5)).isoformat(), now) == "5 min ago"
    assert ago((now - timedelta(hours=30)).isoformat(), now) == "yesterday"
    assert ago((now - timedelta(minutes=58)).isoformat(), now, short=True) == "58m"
    assert ago((now - timedelta(days=3)).isoformat(), now, short=True) == "3d"
    assert ago(None) == "never"
    assert source_agent("agent-claude-code-laptop") == "claude-code"
    assert source_agent("tasks-board") is None
    sources = {"agent-codex-a": {"latest": "2026-10-01"}, "agent-codex-b": {"latest": "2026-10-03"}}
    assert last_seen(sources, "codex") == "2026-10-03"
    p = PALETTES["jezero"]
    assert "JEZERO" not in wordmark(p).plain and "██" in wordmark(p).plain
    assert sparkline([0, 1, 8], p).plain == "·▁█"


def test_people_commands_on_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["find", "retry", "backoff", "--demo"]) == 0
    out = capsys.readouterr().out
    assert "#2412" in out and "2 results" in out
    assert main(["show", "2390", "--newer", "--demo"]) == 0
    out = capsys.readouterr().out
    assert "NEWER ON THIS SUBJECT" in out and "#2412" in out
    assert main(["agents", "--demo"]) == 0
    assert "Codex" in capsys.readouterr().out
    assert main(["find", "nothing-like-this", "--demo"]) == 1
