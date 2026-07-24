# Contributing to gdmutant

Thanks for your interest! gdmutant is early (pre-v0.1): the engine, GDScript adapter, GdUnit4
runner, reporter, and CLI are built and tested — including a live self-test that runs both runner
paths against real Godot in CI. The most useful contributions right now are issues: bug reports,
GDScript patterns that should be mutated, and real-world use cases.

## Development setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project. Install uv (one line — see uv's install
docs), then from the repo root:

```sh
uv sync --frozen      # fetches the pinned Python + installs the exact locked deps into .venv
```

That's the whole setup: uv reads `.python-version` and fetches the pinned Python for you. Run project
commands with `uv run` (e.g. `uv run pytest`).

Optional — a fully pinned toolchain via [mise](https://mise.jdx.dev), which pins uv itself too:
`mise install`, then either activate mise in your shell (`mise activate`) or prefix commands with
`mise exec --` (e.g. `mise exec -- uv sync --frozen`) so `uv` is on PATH.

Optional but recommended: install the git hooks in `.pre-commit-config.yaml` so these checks run
automatically instead of by hand.

```sh
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

This is self-contained — it works the same way for anyone who clones this repo, with no
dependency on the maintainer's personal machine setup. Once installed: a secret scan runs on every
commit, and the full checks below run automatically before every push.

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
