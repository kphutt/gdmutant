---
type: decision
status: active
created: 2026-07-29
---

# Merge-time checks run locally, and the release gate runs live in the cloud, at ship time

## Status
Accepted

## Context
`ci.yml` ran automatically on every `pull_request` and every push to `main`: five jobs
(`secret-scan`, `verify`, `license-check`, `selftest-godot`, `selftest-gut`), each billed a minimum
of 1 Actions minute regardless of how fast it actually ran. At roughly 12 runs/day, on a private
repo's limited included Actions allowance, that adds up fast, on a repo where merging doesn't ship
anything. Only a version tag followed by a human clicking "Publish" on the resulting draft Release
ships a package. Every other merge is just development.

Meanwhile, `publish.yml`'s release-time gate (added to close a real hole, since a maintainer can
create and publish a Release straight from the GitHub web UI, bypassing `release.yml` entirely)
proved CI passed by *querying the Actions API* for `ci.yml`'s history at the exact released commit.
That mechanism has a hard dependency: it only works if `ci.yml` actually ran in the cloud for that
commit. Turning `ci.yml`'s auto-triggers off to fix the cost problem would have broken every future
release, because the lookup would find nothing and fail closed forever, not just on the risky
commits.

`ci.yml`'s own comments already admitted a related, narrower version of this gap. Windows coverage
of `verify` had already moved to the maintainer's own machine (`scripts/verify_local.py`, run by
hand before a release) with the explicit caveat "nothing enforces that mechanically... it is a
discipline, not a gate."

## Decision
Split by *when* a check needs to be unbypassable, not by what it checks:

- Merge-time (`ci.yml`): local only, while private. `on:` is now `workflow_dispatch` only, with no
  automatic trigger. The same checks run via `.pre-commit-config.yaml`'s pre-push stage (gitleaks,
  ruff, gdlint, mypy, pytest, pip-audit, and now `license-check` too, via a wrapper that restores
  dev dependencies afterward). This is discipline, not a mechanical gate, acceptable here because
  nothing ships on a merge.
- Ship-time (`publish.yml`): the sole authoritative, unbypassable gate, live. Its `ci-gate` job
  no longer looks up a separate workflow's history. It runs the checks itself, on the exact released
  commit, immediately before `publish-pypi` is granted `id-token: write`: `verify` on both Linux
  *and* Windows, `license-check`, and both Godot self-tests (`selftest-godot`, `selftest-gut`), so
  six guards total, alongside the two pre-existing provenance checks (tag matches version, commit
  is on main). This also closes the Windows-verify "discipline, not gate" hole: it's a real,
  mechanical check now, on every real release.
- One definition of each check, reused, not restated. The release-time jobs don't reimplement
  `verify` or `license-check`. They call `scripts/verify_local.py --job <name>`, which reads the
  steps out of `ci.yml` and runs them (the mechanism already built for the Windows-local case,
  generalized to take `--job`). The Godot self-tests reuse the existing `scripts/run_selftest.py`
  and `run_selftest_gut.py`. Local pre-commit hooks call the same scripts. Whether a check runs on a
  push, on a developer's machine, or at release time, it is the *same command*, and only where and
  when it executes differs.
- `release.yml` no longer checks CI status at all. It only ever staged a harmless *draft*
  Release (a draft never fires `release: published`, so nothing ships from it). Duplicating the now
  much heavier release-time verification there, with Godot runners on every tag push whether or not
  the tag is ever published, wasn't worth it for a check whose only value was failing a few minutes
  earlier on a bad tag. It keeps its two cheap, git-only provenance guards (version match, ancestry).
- Branch protection's required checks for `ci.yml`'s jobs are removed in this same change, because
  a required check whose workflow no longer fires blocks every PR forever.
- `scripts/require_ci_success.py` (the Actions-API-lookup mechanism) and its tests are deleted,
  retired rather than left dormant.

This is trivial to reverse, by design. Once gdmutant is public, Actions is free and unlimited, and
running `ci.yml` on every PR again is worth it for contributor-facing visibility. Restoring it is
adding back the two trigger lines `ci.yml`'s own header comment documents, plus re-adding the four
job names to required checks, with no job logic changes, because none of it was rewritten to assume
"local-only," only *triggered* differently. `publish.yml`'s live gate is not something to revert at
that point. It stays the authoritative check regardless of whether `ci.yml` also runs on every push.

## Consequences
- Cost. `ci.yml`'s ~2,800 billed min/month (measured, July 2026) drops to whatever
  `workflow_dispatch` is manually invoked, effectively $0 while private. `publish.yml`'s
  gate now runs seven jobs including two Godot runners and a Windows runner, but only at release
  time, which happens on the order of once every few weeks, not ~12 times/day.
- A release is now MORE verified than a merge was, not less. Every release re-runs the full
  suite on the exact commit being shipped, rather than trusting a possibly-stale prior CI run. The
  old mechanism could pass a commit whose `ci.yml` run happened hours or days before the tag, on a
  now-changed branch-protection or dependency state, and the new one can't, because it re-executes
  now.
- Merge-time correctness is no longer mechanically enforced. A bad commit CAN reach `main` if
  a contributor skips or doesn't install the pre-commit hooks. Accepted: nothing ships on merge, and
  the release gate catches it before anything does. This is the standard trade for a private repo
  that ships nothing on merge, and it is the one made here.
- A release now costs more Actions time than the old gate did (six live jobs against one API call),
  but pays for itself immediately given how rare releases are relative to merges, which is the
  entire point of moving the expensive checks to the rare event instead of the frequent one.

## Correction (2026-08-01)

The Decision above describes the pre-push stage as running "the same checks" as `ci.yml`'s `verify`
job, naming `gitleaks, ruff, gdlint, mypy, pytest, pip-audit`. `uv run pytest` has since moved off
that stage, to `stages: [manual]` (`chore: take the test suite off the pre-push stage`, #192,
corrected on [ADR-0009](0009-pre-commit-for-local-dev-checks.md#correction-2026-07-31), which is the
record of the pre-push stage's own contents and is where this correction defers for the reasoning).
Everything else in that list still runs on every push, unchanged.

This does not touch the split this record makes. The suite still runs, just not gated at push time:
by hand through `uv run python scripts/verify_local.py` or `pre-commit run --hook-stage manual
pytest`, and unconditionally in `publish.yml`'s release-time gate, on both Linux and Windows, before
anything ships. That gate is the one this ADR names as the sole unbypassable check, and it is
untouched. "Merge-time correctness is no longer mechanically enforced" already covered exactly this
kind of gap: a contributor who skips the manual suite run reaches `main` unverified on that one axis,
the same way skipping any pre-commit hook always could. The premise this decision rests on, that a
merge does not ship anything and the release gate is what catches it, is unaffected. Only the
pre-push stage's exact command list, quoted above, needed updating.

## Correction (2026-08-04)

`ci.yml`'s `pull_request`/`push` triggers are restored, ahead of gdmutant going public
(`workflow_dispatch` kept alongside them, per this record's own "Trivial to reverse" section). The
cost argument in Context and Decision no longer applies going forward: Actions minutes are free and
unlimited on a public repo, so there is no billed-minute problem left to solve by keeping this
workflow manual-only.

A second reason surfaced that this record didn't originally weigh, because it didn't apply while
gdmutant was private and solo-authored: **merge-time correctness being "local, not mechanically
enforced" only works when every contributor reliably has the local hooks installed.** That was true
by construction while the operator was the only author (their own machines install them fleet-wide).
It stops being true the moment gdmutant can take a pull request from anyone else — nothing forces a
drive-by contributor to run `pre-commit install`, and `CONTRIBUTING.md` already documented, honestly,
that skipping it left most of a PR's checks unrun. Restoring this workflow's automatic triggers is
what makes that section of `CONTRIBUTING.md` false as written, so it was rewritten in the same
change: cloud CI goes back to being the check every PR gets regardless of contributor setup; local
hooks are now correctly described as the fast, optional, catches-it-earlier layer, not "the real
gate."

Everything else stands. `publish.yml`'s release-time gate is still the sole *unbypassable* check —
this restoration adds contributor-facing visibility back, it does not change what's required to
ship. The Decision section's reasoning for the original split (why merge-time and ship-time can
tolerate different rigor) is historical record, not reversed by this correction — only the trigger
configuration it produced changed, and for reasons this record didn't originally anticipate.
