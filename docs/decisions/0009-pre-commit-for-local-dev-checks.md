---
type: decision
status: active
created: 2026-07-23
---

# Standard pre-commit for local dev checks, replacing scripts/local-verify

## Status
Accepted

## Context
An earlier PR (#73) added `scripts/local-verify`, a hand-rolled script mirroring CI's Verify job,
dispatched automatically via a global `core.hooksPath` wired into every repo on the maintainer's own
machine, gated by a hash-pinned "approve once, re-approve on edit" trust store.

That mechanism only ever helps the maintainer: it lives in personal git config, not this repo, so it
does nothing for anyone else who clones gdmutant. A project has to be assumed to eventually get an
outside contributor, so any local-check mechanism needs to be self-contained in the repo itself,
rather than dependent on one person's machine setup. On top of that, the two mechanisms turned out
to actively conflict: `pre-commit` (the standard tool) refuses to install its own hooks at all while
`core.hooksPath` is set globally to something else, confirmed live while prototyping this change.

Options considered:
- (a) Keep the custom global-dispatch and trust-gate system, accepting that it gives contributors
  nothing.
- (b) Husky. Ruled out, because it is npm/JS-only and gdmutant is not a JS project.
- (c) Lefthook. Fast, polyglot, config-as-code, but `lefthook install` actively conflicts with a
  globally-set `core.hooksPath` the same way pre-commit does (same root cause), so it doesn't avoid
  the underlying problem either.
- (d) pre-commit (https://pre-commit.com). Polyglot, the broadest ecosystem of ready-made hooks,
  isolated per-hook environments, and, critically, self-contained: the config ships in the repo, so
  any clone gets identical checks the moment someone runs `pre-commit install`, independent of any
  personal machine setup.

## Decision
Adopt pre-commit, self-contained via `.pre-commit-config.yaml`, replacing `scripts/local-verify`
and its dispatch and trust-gate machinery entirely (PR #73 closed unmerged, superseded by #74):

- `pre-commit` stage: gitleaks, run via `scripts/run_gitleaks.py`. Cheap, and it catches a secret
  before it's even committed rather than only before push. Deliberately not version-pinned: it calls
  whatever `gitleaks` binary is already on the contributor's PATH, skipping gracefully (not failing)
  if none is found. A pinned version (e.g. via the vendored `gitleaks-system` pre-commit hook, which
  builds or requires a specific release) would reintroduce the exact "breaks unless you already have
  the right thing installed" problem this migration exists to remove, so the self-containment goal
  wins over version-pin precision here. (Found in review of PR #74: an earlier draft's ADR text and
  PR description both claimed pinning that wasn't actually true, corrected here rather than
  re-added.)
- `pre-push` stage: the same commands CI's Verify job runs (ruff check/format, gdlint, mypy,
  pytest, pip-audit), not a reimplementation, so local and CI can't drift apart.
- `always_run: true` on every hook. pre-commit's default file-based filtering would otherwise skip a
  hook silently on a commit or push that touched no matching files, under-checking relative to what
  `scripts/local-verify` always did (found live while prototyping: an empty test commit produced zero
  hook output until this was added).
- Two `manual`-stage hooks (`gdmutant-selftest`, `gdmutant-mutation`) preserve the old script's
  opt-in `--with-selftest` and `--with-mutation` flags.
- `pre-commit` itself installs via the existing `mise`/`uv` pinned dev-dependency group, so there is
  no new per-machine install step beyond what already exists.

## Consequences
- Self-contained for anyone. A contributor gets identical local checks with one documented
  command (`pre-commit install --hook-type pre-commit --hook-type pre-push`), independent of the
  maintainer's personal setup, which is the property the old mechanism could never provide.
- Trade-off, accepted deliberately: the old trust gate required explicit human re-approval any
  time the local script's content changed (hash-pinned, direnv-style). pre-commit has no equivalent,
  so code review at merge time is the trust boundary, same as every other pre-commit user. This is
  the standard, industry-normal model, and the maintainer judged the self-containment gain worth
  losing that one property.
- Not automatic for a stranger. Unlike the old global mechanism (zero-touch for the maintainer on
  every repo, forever), pre-commit only runs for someone once they explicitly install it. Documented
  in `README.md` and `CONTRIBUTING.md` so it's discoverable, but nothing forces a contributor to run
  the install command.
- The old global mechanism is not removed by this decision. It simply stops being what gdmutant
  relies on, so this repo's checks no longer depend on anything outside this repo.

## Correction (2026-07-31)

The Decision above lists the `pre-push` stage as

> the same commands CI's Verify job runs (ruff check/format, gdlint, mypy, pytest, pip-audit)

`pytest` has since moved to the `manual` stage. Everything else in that list still runs before every
push, and still comes from `ci.yml` rather than a restatement of it.

The reason is the one this record already argues from: a check nobody runs is not a check. The
suite is the only slow leg of the chain, 35 seconds on the maintainer's machine and 2:40 measured
elsewhere, against six seconds for every other hook combined. That is long enough to exceed
the tool timeout an automated contributor runs under, and two of them hit it. One pushed with
`--no-verify` after running the identical gate by hand, so nothing went unchecked that time, but
`--no-verify` skips *every* hook, secret scan included, and it leaves no trace. A hook that reliably
times out trades a ten-second gate for a silent one.

So the suite runs by hand instead, through `uv run python scripts/verify_local.py`, which replays
the whole Verify job out of `ci.yml`. It also still runs unconditionally in `publish.yml`'s
release-time gate, on both Linux and Windows, before anything ships. That gate is unchanged and
remains the one no local state can skip ([ADR-0012](0012-merge-time-local-ship-time-cloud.md)).

The mirroring property this record was written to protect is intact and is now checked directly.
`tests/test_local_check_parity.py` holds one named exemption, with the reason written beside it, and
a second test proving the exempt hook still exists and still runs `ci.yml`'s own command. Removing
the check rather than moving it fails that test.
