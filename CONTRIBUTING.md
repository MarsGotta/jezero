# Contributing

Thanks for your interest. Biblioteca is early and small on purpose.

1. Open an issue first for anything beyond a small fix, so we can agree on the approach.
2. Keep changes focused, with tests. Bug fixes come with a regression test.
3. Run the checks before opening a pull request:
   `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format --check src tests`
   and `python3 scripts/privacy_check.py --all`.
4. Enable the privacy hook once per clone: `git config core.hooksPath .githooks`.
   Add a git-ignored `.privacy-denylist` with terms that must never be committed from
   your machine (host names, people, employers), one per line.
5. Never include real transcripts, emails or personal details in issues, tests or
   fixtures. Use synthetic data.

By contributing you agree that your contributions are licensed under the Apache-2.0
license of this project.
