---
type: decision
status: active
created: 2026-07-30
---

# Local mutation testing on Windows uses poodle, not mutmut or cosmic-ray

## Status
Accepted

## Context
`mutmut` (the tool CI dogfoods gdmutant with, see `docs/mutation-testing.md`) has no native Windows
support — it depends on `os.fork()` to reuse an already-imported pytest process per mutant, which is
also why it's fast. Its own CLI tells Windows users to use WSL ([boxed/mutmut#397][397] is open, the
draft fix ([#404][404]) was abandoned by its own author: reusing worker processes across mutants is
"brittle," since a mutant that corrupts global state poisons every mutant tested after it in the same
process — exactly what `fork()`-per-mutant was avoiding).

`cosmic-ray` was tried as the native-Windows replacement (real Windows support, actively maintained).
It works, but measured on this machine it's architecturally much slower than mutmut per mutant — every
mutant pays a fresh subprocess start + import + test-collection cost mutmut's fork model skips. Worse,
its `local` distributor is sequential by design, and its `http` distributor requires **provisioning a
separate full code copy per worker** ([its own tutorial][cr-dist] does this manually: `mkdir worker1; cp
mod.py worker1; ...`) — running several workers against one shared tree, as first tried here, is a
misconfiguration its own docs call out, and it is exactly what caused a real incident: an
interrupted/backgrounded `cosmic-ray` run left a mutation applied and unreverted in
`gdmutant/engine/spans.py`, on disk, in the real working tree — twice. Git Bash's process tools
(`ps`/`jobs`/`kill`) couldn't even see the native Windows processes responsible; `tasklist`/`taskkill
/F` was required to stop them.

## Decision
**CI keeps mutmut, unchanged** (Linux runner, no fork() problem there — see `docs/mutation-testing.md`
for that job). **The local, opt-in Windows hook switches to [poodle][poodle].** poodle copies each
mutant into a temp folder (`.poodle-temp/run-<id>/`, see `poodle/run.py`) and runs the suite from
*that* copy — the real source tree is never the mutation target, so a hard kill can't corrupt it by
construction, not by discipline. Verified directly on this machine: killed a live 6-worker poodle run
mid-flight (`taskkill /F` on every python process) and confirmed (a) `git status` was clean
immediately, no restore step needed, and (b) no orphaned pytest/python processes were left running.

Two real config traps found getting a valid baseline, left here so they aren't rediscovered:
- `source_folders` must be poodle's *container* directory (`["."]`  here), not the package directory
  itself (`["gdmutant"]`) — set to the package name directly, poodle's runner never `cd`s into the
  temp copy (`run.py`'s `run_cwd` only switches when `source_folder.resolve() == cwd`), so pytest
  silently runs the real, unmutated tree and every mutant reports "not found."
- `file_copy_flags` needs `DOTGLOB` added to the default (`GLOBSTAR | DOTGLOB | NODIR` = `16704`) —
  wcmatch's default glob skips dotfiles, so `.github/` never reaches the temp copy and any test
  reading it (`test_check_release_tag.py`) fails on a missing file, not the mutation.

`scripts/check_mutation_baseline.py` scopes poodle to files changed vs `origin/main` (`--only` per
file) rather than sweeping the whole package — a full `gdmutant/` sweep is ~2,600 mutants, which even
at poodle's real measured rate (~2.3s/mutant with 6 workers) is well over an hour, wrong for a manual
pre-push hook. Diff-scoping mutation testing is the mainstream answer to this exact tradeoff (Google:
["State of Mutation Testing at Google"][google-mt], ICSE-SEIP 2018; Stryker's [`--since`][stryker-since]).

## Consequences
- Local mutation testing on Windows works again, at a real, sane score (spot-checked:
  87.8%, 72/82 caught, on `gdmutant/engine/spans.py` — a legitimate number, not the 0% or invalid
  >100%-parallel numbers from the two misconfigured tools above).
- Two mutation *tools* now exist in the repo (mutmut for CI, poodle for local) instead of one. They
  will not produce identical scores on the same file — different operator sets — and that's accepted:
  the local run's job is "did my change just break test coverage," not "match CI's number."
- poodle is a small, single-maintainer project (~1,500 downloads/month) — a real supply-chain tradeoff
  next to mutmut's much larger install base. Accepted for a dev-only, advisory tool; revisit if it goes
  quiet the way `mutatest` did (evaluated and rejected here for exactly that reason — last released
  2022-02, incompatible with 3.11+ bytecode).
- `cosmic-ray` is removed entirely — not kept as a fallback. Its dependency, config file
  (`cosmic-ray.toml`), and the transitive `aiohttp`/`sqlalchemy`/etc. tree it pulled in are gone.

[397]: https://github.com/boxed/mutmut/issues/397
[404]: https://github.com/boxed/mutmut/pull/404
[cr-dist]: https://cosmic-ray.readthedocs.io/en/latest/tutorials/distributed/
[poodle]: https://github.com/WiredNerd/poodle
[google-mt]: https://research.google/pubs/state-of-mutation-testing-at-google/
[stryker-since]: https://github.com/stryker-mutator/stryker-js/blob/master/docs/incremental.md
