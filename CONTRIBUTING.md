# Contributing to gdmutant

Thanks for your interest! gdmutant is early (pre-v0.1): the engine, GDScript adapter, GdUnit4
runner, reporter, and CLI are built and tested — including a live self-test that runs both runner
paths against real Godot in CI. The most useful contributions right now are issues: bug reports,
GDScript patterns that should be mutated, and real-world use cases.

## Development setup

The toolchain is pinned so setup is one command per machine.

```sh
# 1. Install mise once (https://mise.jdx.dev), then:
mise install          # installs the pinned Python + uv
uv sync --frozen      # installs the exact locked dependencies into .venv
```

Prefer not to use mise? Install [uv](https://docs.astral.sh/uv/) yourself, then `uv sync --frozen`.

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
