"""Which project a session belongs to, from the folder it ran in.

Shared by the server connectors and the device client (copied next to it as
`jezero_projects.py`), so it uses the standard library only and runs on Python 3.9.

Rules, first match wins:

1. `paths`: explicit folders -> project (`"~/work/acme-web" = "acme"`). `~` matches any
   home folder (`/Users/<u>`, `/home/<u>`, `C:\\Users\\<u>`), so one rule works for every
   computer. The longest matching folder wins.
2. The git repository: the name of the folder that holds `.git`. For a worktree (a `.git`
   file) it is the name of the main repository, so `app-wt-fix` still counts as `app`.
   Needs the folder to exist on this computer; sessions uploaded from other computers
   carry the answer their client found (a `<session file>.project` file next to it).
3. `roots`: folder names that hold projects (`projects`, `code`, `dev`...), case
   insensitive: the folder right below the first one is the project.
4. Nothing: the summariser decides from the text.

`aliases` rename the result (`"atlas-wt" = "atlas"`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

DEFAULT_ROOTS: tuple[str, ...] = (
    "projects",
    "code",
    "dev",
    "src",
    "repos",
    "repositories",
    "git",
    "github",
    "gitlab",
    "workspace",
    "workspaces",
    "work",
    "developer",
    "sites",
)
"""Common names of the folder people keep their projects in."""

_GENERIC = {
    *DEFAULT_ROOTS,
    "worktrees",
    "app",
    "apps",
    "packages",
    "tmp",
    "temp",
    "desktop",
    "documents",
    "downloads",
}
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE)
SIDECAR_SUFFIX = ".project"


def _parts(path: str) -> tuple[str, ...]:
    if re.match(r"^[A-Za-z]:[\\/]", path) or "\\" in path:
        return PureWindowsPath(path).parts
    return PurePosixPath(path).parts


def _home_length(parts: tuple[str, ...]) -> int:
    """How many leading parts are the home folder (0 if the path is not under one)."""
    lowered = [p.lower().rstrip("\\/") for p in parts]
    if len(parts) >= 3 and lowered[0] in ("/", "") and lowered[1] in ("users", "home"):
        return 3
    if len(parts) >= 3 and re.match(r"^[a-z]:\\?$", lowered[0]) and lowered[1] == "users":
        return 3
    if len(parts) >= 2 and lowered[0] == "/" and lowered[1] == "root":
        return 2
    return 0


def _usable(name: str) -> bool:
    return bool(_NAME.match(name)) and not _UUIDISH.match(name) and name.lower() not in _GENERIC


def _norm(name: str) -> str:
    return name.lower()


class ProjectRules:
    def __init__(
        self,
        *,
        paths: dict[str, str] | None = None,
        roots: Iterable[str] | None = None,
        aliases: dict[str, str] | None = None,
        git: bool = True,
    ) -> None:
        self.paths = []
        for folder, project in (paths or {}).items():
            parts = _parts(str(folder).rstrip("/\\"))
            home = parts[0] == "~"
            key = tuple(p.lower() for p in (parts[1:] if home else parts))
            self.paths.append((home, key, str(project)))
        self.paths.sort(key=lambda p: -len(p[1]))
        self.roots = {r.lower() for r in (DEFAULT_ROOTS if roots is None else roots)}
        self.aliases = {_norm(k): v for k, v in (aliases or {}).items()}
        self.git = git

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, aliases: dict[str, str] | None = None):
        data = dict(data or {})
        merged = dict(data.get("aliases") or {})
        merged.update(aliases or {})
        return cls(
            paths=data.get("paths"),
            roots=data.get("roots"),
            aliases=merged,
            git=bool(data.get("git", True)),
        )

    def with_aliases(self, aliases: dict[str, str] | None) -> ProjectRules:
        if not aliases:
            return self
        clone = ProjectRules(roots=self.roots, git=self.git)
        clone.paths = list(self.paths)
        clone.aliases = {**self.aliases, **{_norm(k): v for k, v in aliases.items()}}
        return clone

    # -- the rules -------------------------------------------------------------------

    def project_for(self, cwd: str | None, session_file: Path | None = None) -> str | None:
        found = read_sidecar(session_file) if session_file else None
        if not found and cwd:
            found = (
                self._from_paths(cwd) or (self.git and git_project(cwd)) or self._from_roots(cwd)
            )
        if not found:
            return None
        return self.aliases.get(_norm(found), _norm(found))

    def _from_paths(self, cwd: str) -> str | None:
        parts = _parts(cwd)
        lowered = tuple(p.lower().rstrip("\\/") for p in parts)
        home = _home_length(parts)
        for under_home, key, project in self.paths:
            if under_home:
                if home and lowered[home : home + len(key)] == key:
                    return project
            elif lowered[: len(key)] == tuple(k.rstrip("\\/") for k in key):
                return project
        return None

    def _from_roots(self, cwd: str) -> str | None:
        parts = _parts(cwd)
        for i, part in enumerate(parts[:-1]):
            if part.lower() in self.roots:
                for name in parts[i + 1 :]:  # skip `worktrees`, `apps`...
                    if _usable(name):
                        return name
                    if name.lower() not in _GENERIC:
                        break
        return None


def git_project(cwd: str) -> str | None:
    """Name of the git repository holding `cwd`, if that folder exists here."""
    try:
        here = Path(cwd)
        if not here.is_dir():
            return None
        home = Path.home().resolve()
        for folder in (here, *here.parents):
            if folder == home or folder.parent == folder:
                return None  # a repository at home or at / is not a project
            marker = folder / ".git"
            if marker.is_dir():
                return folder.name if _usable(folder.name) else None
            if marker.is_file():
                main = _worktree_main(marker)
                name = main.name if main else folder.name
                return name if _usable(name) else None
    except OSError:
        return None
    return None


def _worktree_main(marker: Path) -> Path | None:
    """`.git` file `gitdir: <repo>/.git/worktrees/<x>` -> `<repo>`."""
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^gitdir:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return None
    gitdir = PurePath(match.group(1).strip())
    parts = gitdir.parts
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i >= 1 and parts[i - 1] == ".git":
            return Path(*parts[: i - 1])
    return None


def read_sidecar(session_file: Path) -> str | None:
    """Project found by the client on the computer the session came from."""
    try:
        text = Path(str(session_file) + SIDECAR_SUFFIX).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if _NAME.match(text) else None


DEFAULT_RULES = ProjectRules()
"""Rules with no configuration: git, then the default root folder names."""

__all__ = ["DEFAULT_ROOTS", "DEFAULT_RULES", "SIDECAR_SUFFIX", "ProjectRules", "git_project"]
