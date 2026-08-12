---
type: how-to
status: active
created: 2026-07-10
---

# Contributing to gdmutant

gdmutant's engine, GDScript adapter, runners, reporter, and CLI are built and tested. The most
useful contributions are issues: bug reports, GDScript patterns that should be mutated, and
real-world use cases. Pull requests are welcome too, especially small, well-scoped fixes. The
workflow below covers the whole process.

Taking part here (issues, pull requests, discussion) means agreeing to the
[Code of Conduct](CODE_OF_CONDUCT.md).

Before writing any code, check [`AGENTS.md`'s "Non-goals
(v0.1)"](AGENTS.md#non-goals-v01) section for what's explicitly out of scope right now (coverage-
gated mutant selection, an LLM-semantic mode, a second-language adapter). For anything nontrivial
that isn't already covered there, open an issue first to talk it through, before sinking real time
into a PR that might not fit.

## Development setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project. Follow [`AGENTS.md`](AGENTS.md) under
"Setup" to install uv and sync the pinned toolchain (`uv sync --frozen`), then run commands with
`uv run` (e.g. `uv run pytest`).

### Install the local checks: fast, optional feedback

`ci.yml` runs automatically on every pull request and push to `main`: lint, types, tests, audit,
license, and a secret scan, all in the cloud, whether or not you set anything up locally. That's
the real gate. The git hooks in `.pre-commit-config.yaml` exist to catch the same problems earlier,
on your own machine, before you even push, not because the cloud check is otherwise missing.
Installing them is optional but saves a round trip to the cloud run. Install them:

```sh
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Once installed, a secret scan runs on every commit, and lint, format, GDScript lint, types, audit
and the license check run before every push. That takes six seconds.

The test suite is deliberately not in that set. It is the only slow leg, and a push hook that takes
minutes gets bypassed with `--no-verify`, which silently skips every other hook too, secret scan
included. Run it yourself before you open a pull request, with the command in the next section.

Install `gitleaks` too. Every other hook runs out of the synced virtualenv, but `gitleaks` is a
standalone binary that `uv sync` does not provide, and `scripts/run_gitleaks.py` prints a skip
message and exits 0 when it is not on `PATH`. Left uninstalled, the hook is green and scanning
nothing. Get it from [the gitleaks install
instructions](https://github.com/gitleaks/gitleaks#installing), then confirm with `uv run pre-commit
run gitleaks`.

The one check nothing, local or cloud, skips is the release-time gate (`publish.yml`), which
re-runs everything live before a real release, not on every merge. See
[ADR-0012](docs/decisions/0012-merge-time-local-ship-time-cloud.md) for why merge-time and
release-time are held to different rigor.

## Before you open a PR

Run this whether or not you installed the hooks. It is the only local command that runs the whole
Verify job, the test suite included:

```sh
uv run python scripts/verify_local.py                       # lint, format, GDScript lint, types, tests, audit
uv run python scripts/verify_local.py --job license-check   # the license gate
uv run python scripts/verify_local.py --list                # print a job's commands without running them
```

That script reads its commands out of `ci.yml` rather than restating them, so it cannot drift from
what the hooks and the release-time gate run. [`AGENTS.md`](AGENTS.md) under "Build · test" spells
the individual commands out, which is what you want when you only need one of them.

If your change adds or touches pure logic, run a local mutation-test pass too, advisory and
opt-in, not part of `verify_local.py`:

```sh
uv run pre-commit run gdmutant-mutation --hook-stage manual
```

This is the same standard [`docs/mutation-testing.md`](docs/mutation-testing.md) holds this
project's own suite to: green tests prove they don't fail, a mutation pass proves a bug on that
line would actually be caught.

The live self-test (`tests/test_selftest_live.py`) auto-skips unless you opt in with a real Godot.
It has separate GdUnit4 and GUT cases, and each one skips on its own if its addon isn't installed,
so install both to actually run the whole file rather than half of it silently:

```sh
python scripts/install_gdunit4.py                            # download + verify the GdUnit4 addon
python scripts/install_gut.py                                # download + verify the GUT addon
GDMUTANT_GODOT=/path/to/godot uv run pytest tests/test_selftest_live.py -v --no-cov
```

## Pull request guidelines

- One focused change per PR, with a clear description of what and why.
- Lint, types, tests, audit, and secret scan all pass. `ci.yml` checks all of this in the cloud on
  every PR regardless, so it's covered either way. The pre-commit and pre-push hooks just surface
  the same failures locally, sooner (see "Install the local checks" above).
- New behavior comes with tests. This is a testing tool, and we hold ourselves to it.
- Larger design changes are recorded as an ADR in `docs/decisions/` (append-only, see the
  existing records for the format) and, where relevant, reflected in `docs/design/`.
- Commits and PRs carry no AI co-author trailer.
- Add any new third-party dependency to `docs/credits.md` with its license.

## Automated review

Opening a PR triggers an automated advisory review that posts comments directly on the PR. It's a
second look for issues that lint, types, and tests don't catch. It's advisory only: it doesn't
block merging, and you don't have to act on every comment. Because of how it authenticates, its
comments appear under the maintainer's own GitHub account. Each one is signed to make clear it's
automated, not the maintainer reviewing by hand.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
