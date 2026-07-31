---
type: how-to
status: active
created: 2026-07-10
---

# Contributing to gdmutant

gdmutant is early (pre-v0.1): the engine, GDScript adapter, runners, reporter, and CLI are built and
tested. The most useful contributions right now are issues — bug reports, GDScript patterns that
should be mutated, and real-world use cases.

## Development setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project. Follow [`AGENTS.md`](AGENTS.md) under
**Setup** to install uv and sync the pinned toolchain (`uv sync --frozen`), then run commands with
`uv run` (e.g. `uv run pytest`).

**Install the local checks — they're the real gate.** `ci.yml` doesn't run automatically on pull
requests right now (see
[ADR-0012](docs/decisions/0012-merge-time-local-ship-time-cloud.md)); merge-time checks run on your
own machine instead, via the git hooks in `.pre-commit-config.yaml`. They run the same commands CI
would, not a reimplementation, and are self-contained (no personal-machine setup). Install them:

```sh
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Once installed, a secret scan runs on every commit and the full checks (lint, types, tests, audit,
license check) run before every push. **If you skip this install step, nothing automated checks
your commits before they reach `main`** — the one check nothing local can skip is the release-time
gate (`publish.yml`), which re-runs everything live before a real release, not on every merge.

## Before you open a PR

If you didn't install the hooks above, run the same checks by hand — the exact command list is in
[`AGENTS.md`](AGENTS.md) under **Build · test** (`ruff` lint + format, `mypy`, `pytest`,
`pip-audit`).

The live self-test (`tests/test_selftest_live.py`) auto-skips unless you opt in with a real Godot.
To run it locally:

```sh
python scripts/install_gdunit4.py                            # download + verify the GdUnit4 addon
GDMUTANT_GODOT=/path/to/godot uv run pytest tests/test_selftest_live.py -v --no-cov
```

## Pull request guidelines

- **One focused change per PR**, with a clear description of what and why.
- **Lint, types, tests, audit, and secret scan all pass** — the pre-commit and pre-push hooks
  enforce this automatically if you installed both above (the secret scan runs at commit time, the
  rest before a push); otherwise run the commands by hand before opening the PR. There's no
  automated PR check today (see "Install the local checks" above).
- New behavior comes **with tests** — this is a testing tool; we hold ourselves to it.
- Larger design changes are recorded as an **ADR** in `docs/decisions/` (append-only; see the
  existing records for the format) and, where relevant, reflected in `docs/design/`.
- Commits and PRs carry **no AI co-author trailer**.
- Add any new third-party dependency to `docs/credits.md` with its license.

## Automated review

Opening a PR triggers an automated advisory review that posts comments directly on the PR. It's a
second look for issues that lint, types, and tests don't catch. It's advisory only — it doesn't
block merging, and you don't have to act on every comment. Because of how it authenticates, its
comments appear under the maintainer's own GitHub account; each one is signed to make clear it's
automated, not the maintainer reviewing by hand.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
