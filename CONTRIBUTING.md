# Contributing to gdmutant

gdmutant is early (pre-v0.1): the engine, GDScript adapter, GdUnit4 runner, reporter, and CLI are
built and tested — including a live self-test that runs both runner paths against real Godot in CI.
The most useful contributions right now are issues: bug reports, GDScript patterns that should be
mutated, and real-world use cases.

## Development setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project. Install uv (one line — see uv's install
docs), then from the repo root:

```sh
uv sync --frozen      # fetches the pinned Python + installs the exact locked deps into .venv
```

That's the whole setup — uv reads `.python-version` to fetch the interpreter. Run project commands
with `uv run` (e.g. `uv run pytest`).

GitHub Actions is the authoritative gate: every PR must pass CI to merge. The git hooks in
`.pre-commit-config.yaml` are an **optional** local mirror of those same checks, so you catch
problems before pushing — they never affect what CI enforces. They're self-contained: no dependency
on any personal machine setup. To install them:

```sh
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Once installed, a secret scan runs on every commit and the full checks below run before every push.

## Before you open a PR

If you didn't install the hooks above, run the same checks CI runs by hand:

```sh
uv run ruff check .        # lint
uv run ruff format .       # format (CI checks this with --check)
uv run mypy gdmutant       # type check
uv run pytest              # tests + coverage
uv run pip-audit           # dependency vulnerability audit
```

The live self-test (`tests/test_selftest_live.py`) auto-skips unless you opt in with a real Godot.
To run it locally:

```sh
scripts/install-gdunit4.sh                                   # download + verify the GdUnit4 addon
GDMUTANT_GODOT=/path/to/godot uv run pytest tests/test_selftest_live.py -v --no-cov
```

## Pull request guidelines

- **One focused change per PR**, with a clear description of what and why.
- **All CI checks must pass** (lint, types, tests, audit, secret scan).
- New behavior comes **with tests** — this is a testing tool; we hold ourselves to it.
- Larger design changes are recorded as an **ADR** in `docs/decisions/` (append-only; see the
  existing records for the format) and, where relevant, reflected in `docs/design/`.
- Commits and PRs carry **no AI co-author trailer**.
- Add any new third-party dependency to `CREDITS.md` with its license.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
