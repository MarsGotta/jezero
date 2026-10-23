"""Release supply chain: the client bundle ships only tracked files, and the app installs
plugins from its own release tag."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_clients.py"
HAS_GIT = shutil.which("git") is not None


def _load_script():
    spec = importlib.util.spec_from_file_location("build_clients_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_repo(tmp_path: Path, git: bool) -> Path:
    """A minimal copy of the repository: the script, clients/ and projects.py."""
    build = _load_script()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / "build_clients.py")
    for rel in build.CLIENT_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    (repo / "src" / "jezero").mkdir(parents=True)
    shutil.copy2(ROOT / "src" / "jezero" / "projects.py", repo / "src/jezero/projects.py")
    if git:
        (repo / ".gitignore").write_text(".env\nnode_modules/\n*.bak\n")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    return repo


def _build(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1767225600"}
    return subprocess.run(
        [sys.executable, "scripts/build_clients.py", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _bundle_names(repo: Path) -> list[str]:
    with tarfile.open(repo / "dist" / "jezero-client.tar.gz") as tar:
        return tar.getnames()


@pytest.mark.skipif(not (ROOT / ".git").exists() or not HAS_GIT, reason="needs a git checkout")
def test_client_files_list_matches_git() -> None:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "clients/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == sorted(_load_script().CLIENT_FILES)


@pytest.mark.skipif(not HAS_GIT, reason="needs git")
def test_untracked_and_ignored_files_stop_the_build(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, git=True)
    (repo / "clients/opencode/.env").write_text("TOKEN=not-a-real-secret\n")
    (repo / "clients/opencode/node_modules/pkg").mkdir(parents=True)
    (repo / "clients/opencode/node_modules/pkg/index.js").write_text("//\n")
    (repo / "clients/hermes/plugin.yaml.bak").write_text("old\n")
    (repo / "clients/forgotten.md").write_text("not added\n")

    for args in ((), ("--check",)):
        result = _build(repo, *args)
        assert result.returncode != 0
        for name in ("opencode/.env", "node_modules/pkg/index.js", "plugin.yaml.bak", "forgotten"):
            assert name in result.stderr
    assert not (repo / "dist" / "jezero-client.tar.gz").exists()
    assert not (repo / "plugins").exists()

    shutil.rmtree(repo / "clients/opencode/node_modules")
    for rel in ("clients/opencode/.env", "clients/hermes/plugin.yaml.bak", "clients/forgotten.md"):
        (repo / rel).unlink()
    (repo / "clients/hermes/__pycache__").mkdir()
    (repo / "clients/hermes/__pycache__/x.cpython-313.pyc").write_bytes(b"\0")
    (repo / "clients/.ruff_cache/0.1").mkdir(parents=True)  # what `ruff check clients` leaves
    (repo / "clients/.ruff_cache/0.1/entry").write_bytes(b"\0")
    result = _build(repo)
    assert result.returncode == 0, result.stderr
    names = _bundle_names(repo)
    assert "jezero-client/opencode/jezero.ts" in names
    assert not [n for n in names if ".env" in n or "_cache" in n or n.endswith(".bak")]
    assert not list((repo / "plugins").rglob("__pycache__"))
    assert _build(repo, "--check").returncode == 0


@pytest.mark.skipif(not HAS_GIT, reason="needs git")
def test_a_new_tracked_file_must_be_listed(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, git=True)
    (repo / "clients/codex/extra.json").write_text("{}\n")
    subprocess.run(["git", "add", "clients/codex/extra.json"], cwd=repo, check=True)
    result = _build(repo)
    assert result.returncode != 0
    assert "not in CLIENT_FILES: clients/codex/extra.json" in result.stderr


def test_outside_git_only_the_allowlist_is_read(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, git=False)
    (repo / "clients/opencode/.env").write_text("TOKEN=not-a-real-secret\n")
    result = _build(repo)
    assert result.returncode != 0 and "clients/opencode/.env" in result.stderr

    (repo / "clients/opencode/.env").unlink()
    result = _build(repo)
    assert result.returncode == 0, result.stderr
    assert "jezero-client/hermes/plugin.yaml" in _bundle_names(repo)

    (repo / "clients/skill/SKILL.md").unlink()
    result = _build(repo)
    assert result.returncode != 0 and "clients/skill/SKILL.md is missing" in result.stderr


def test_bundle_is_reproducible_and_its_sha256_is_printed(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, git=False)
    first = _build(repo)
    assert first.returncode == 0, first.stderr
    bundle = repo / "dist" / "jezero-client.tar.gz"
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert f"sha256 {digest}  jezero-client.tar.gz" in first.stdout
    assert re.search(rf"^sha256 {digest}  jezero-client.tar.gz$", _build(repo).stdout, re.M)
    with tarfile.open(bundle) as tar:
        members = tar.getmembers()
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members} == {
        (0, 0, "", "", 1767225600)
    }
    assert [m.name for m in members] == sorted(m.name for m in members)


# ----------------------------------------------------------------------------- app plans


def test_release_ref() -> None:
    from jezero.ui.data import release_ref

    assert release_ref("0.1.0") == "v0.1.0"
    assert release_ref("0.2.0rc1") == "v0.2.0rc1"
    assert release_ref("1.0.0.post1") == "v1.0.0.post1"
    assert release_ref("0.1.0.dev0") is None
    assert release_ref("0.1.0+local") is None


def _status(agent: str, installed: bool):
    from jezero.ui.data import AgentStatus

    return AgentStatus(agent, agent, True, installed, "0.0.1" if installed else None, "9.9.9")


@pytest.mark.parametrize("installed", [False, True])
def test_a_release_installs_plugins_from_its_tag(
    monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    from jezero.ui import data

    monkeypatch.setattr(data, "REF", "v0.1.0")
    claude = [cmd for cmd, _ in _status("claude-code", installed).plan]
    assert claude[:3] == [
        ["claude", "plugin", "marketplace", "remove", "jezero"],
        ["claude", "plugin", "marketplace", "add", "MarsGotta/jezero#v0.1.0"],
        ["claude", "plugin", "install", "jezero@jezero"],
    ]
    assert len(claude) == (3 if installed else 4)
    codex = _status("codex", installed).plan
    assert [cmd for cmd, _ in codex][:3] == [
        ["codex", "plugin", "marketplace", "remove", "jezero"],
        ["codex", "plugin", "marketplace", "add", "MarsGotta/jezero@v0.1.0"],
        ["codex", "plugin", "add", "jezero@jezero"],
    ]
    assert [ok for _, ok in codex][:3] == [True, False, False]
    agy = _status("antigravity", installed).plan
    assert agy == [
        (
            [
                "agy",
                "plugin",
                "install",
                "https://github.com/MarsGotta/jezero/tree/v0.1.0/plugins/antigravity",
            ],
            False,
        )
    ]
    hermes = " ".join(_status("hermes", installed).plan[0][0])
    assert "v0.1.0" not in hermes  # Hermes pins only to a commit SHA: it follows main


def test_a_dev_build_follows_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from jezero.ui import data

    monkeypatch.setattr(data, "REF", None)
    assert _status("claude-code", False).plan[0][0] == [
        "claude",
        "plugin",
        "marketplace",
        "add",
        "MarsGotta/jezero",
    ]
    assert _status("antigravity", False).plan[0][0][-1].endswith("/tree/main/plugins/antigravity")
