#!/usr/bin/env python3
"""Pre-commit privacy check: block commits that leak personal details.

Two layers:
1. Built-in patterns that are almost never wanted in a public repo (absolute home
   directories, private-network addresses, credential shapes).
2. A private, git-ignored `.privacy-denylist` file with one term per line (host names,
   people, employers, projects). Matching is case-insensitive. Lines starting with `#`
   are comments.

Usage: `python scripts/privacy_check.py` checks staged files (what the hook runs);
`--all` checks every tracked file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / ".privacy-denylist"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".lock"}
ALLOW_MARKER = "privacy: allow"

BUILTIN = [
    (
        "home directory path",
        re.compile(r"/(?:home|Users)/(?!user\b|you\b|me\b)[a-z][a-z0-9_-]+/"),
    ),
    (
        "CGNAT/tailnet address",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("private LAN address", re.compile(r"\b(?:192\.168|10\.\d{1,3})\.\d{1,3}\.\d{1,3}\b")),
    (
        "private LAN address",
        # 172.16.0.0/12 only: a full dotted quad, second octet 16-31, not part of a longer
        # number or version string (1.172.16.0.4).
        re.compile(r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"),
    ),
    (
        "credential",
        re.compile(r"\b(?:sk-ant-|ghp_|github_pat_|xox[abposr]-|AKIA)[\w\-]{8,}"),
    ),
]


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def _staged_text(path: str) -> str:
    out = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def _denylist() -> list[re.Pattern[str]]:
    if not DENYLIST.is_file():
        return []
    terms = [t.strip() for t in DENYLIST.read_text(encoding="utf-8").splitlines()]
    return [re.compile(re.escape(t), re.IGNORECASE) for t in terms if t and not t.startswith("#")]


def scan(paths: list[str], *, staged: bool) -> list[str]:
    private = _denylist()
    problems: list[str] = []
    for path in paths:
        if Path(path).suffix.lower() in SKIP_SUFFIXES or path == "scripts/privacy_check.py":
            continue
        try:
            text = _staged_text(path) if staged else (ROOT / path).read_text(encoding="utf-8")
        except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                continue
            for label, pattern in BUILTIN:
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: {label}")
            for pattern in private:
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: private term from .privacy-denylist")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="scan all tracked files")
    args = parser.parse_args()
    if args.all:
        paths = _git("ls-files")
    else:
        paths = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    problems = scan(paths, staged=not args.all)
    if problems:
        print("privacy check failed (the matched text is not printed):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(f"Fix it, or add '{ALLOW_MARKER}' to the line if it is intentional.", file=sys.stderr)
        return 1
    print(f"privacy check passed ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
