# Contributing

Thanks for your interest. Jezero is early and small on purpose.

1. Open an issue first for anything beyond a small fix, so we can agree on the approach.
2. Keep changes focused, with tests. Bug fixes come with a regression test.
3. Run the checks listed in [AGENTS.md](AGENTS.md) before opening a pull request:
   `uv run pytest`, `uv run ruff check src tests scripts clients`,
   `uv run ruff format --check src tests scripts clients` and
   `python3 scripts/privacy_check.py --all`. The device client must keep running on
   Python 3.9 with the standard library only (`clients/`, `src/jezero/projects.py`).
4. Enable the privacy hook once per clone: `git config core.hooksPath .githooks`.
   Add a git-ignored `.privacy-denylist` with terms that must never be committed from
   your machine (host names, people, employers), one per line.
5. Never include real transcripts, emails or personal details in issues, tests or
   fixtures. Use synthetic data.

## Documentation

The docs live in [`docs/`](docs/README.md) and follow [docs/STYLE.md](docs/STYLE.md):
tutorials, how-to guides, reference and explanation, each in its folder. A change that
users can see comes with its docs in the same pull request.

- `docs/reference/cli.md` and `docs/reference/configuration.md` are generated from the
  code. Change the help text or the docstring, then run
  `uv run python scripts/make_reference.py`.
- `docs/app/*.svg` are real screenshots of the app on demo data:
  `uv run python scripts/make_screenshots.py`.
- `uv run python scripts/check_docs.py` checks every relative link and anchor. CI runs
  all three checks.

By contributing you agree that your contributions are licensed under the Apache-2.0
license of this project.

## Adding a connector

A connector is one file in `src/jezero/connectors/`. It reads a source incrementally
(only what changed since the cursor in `ConnectorState`) and returns `RawDoc`s; the
pipeline does redaction, summarising and storage. End the module with its type:

```python
def _build(conn: ConnectorConfig, ctx: BuildContext) -> MyConnector:
    return MyConnector(
        conn.name,
        conn.options,
        max_segment_chars=ctx.max_segment_chars,
        default_project=conn.project,
        project_rules=ctx.project_rules,  # if it knows the working directory
    )


CONNECTOR_TYPE = ConnectorType("mytool", "available", "What it reads.", build=_build)
```

That is all the registration there is: `jezero check-config` lists it and `jezero run`
can schedule it. Add a test with synthetic files (see `tests/test_codex.py`), and an
example entry in `jezero.example.toml`. New connectors should start with
`enabled = false` in examples, so users run a pilot (`jezero run --connector NAME
--sample 3`) before a full load.

A connector can also live in its own package: expose its `ConnectorType` through the
`jezero.connectors` entry point group and Jezero picks it up once installed.

