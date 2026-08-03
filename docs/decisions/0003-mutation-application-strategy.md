---
type: decision
status: active
created: 2026-07-11
---

# Apply mutants on disk, one at a time, with restore

## Status
Accepted, corrected 2026-07-31: one Consequences bullet claimed more crash safety than the
mechanism can deliver. The decision itself stands. The bullet is restated below and the original
wording preserved under [Correction](#correction-2026-07-31).

## Context
The runner runs the target project's real test suite (`godot --headless` plus GdUnit4), which loads
source from disk (`res://` files). So a mutant must be materialized on disk for the suite to
exercise it. Options:

- (a) Write the mutated file → run the suite → restore the original.
- (b) Mutant schemata, or "switching": bake all mutants into one instrumented copy and toggle
  each via a runtime switch (the PIT optimization). Fast (parse and instrument once) but complex and
  language-specific.
- (c) Copy the whole project per mutant: safe isolation, but slow and wasteful.

## Decision
Use (a): on disk, one mutant at a time, with the original restored in a `finally`. For each
mutant the engine writes the mutated source to the file, runs the suite, and restores the original,
even if the runner raises. Each mutant is applied in isolation (FG-1.2), and the project is never
left mutated. Invalid mutants (NF-5) are classified without ever touching disk. Simple and correct
for v0.1's full-suite-per-mutant model.

## Consequences
- One file write and one full suite run per mutant. Booting Godot per mutant is slow, so the perf
  path is coverage-gated selection and, later, schemata (b), both deferred to Tier B (`DESIGN.md`
  §5). This decision does not preclude adopting (b) later. It is the simple correct baseline.
- A runner exception mid-run still restores the file (`finally`), so a failed *run* never leaves
  the project mutated. This covers what the engine can catch: a runner that raises, a suite that
  times out, any exception that propagates through the interpreter. It does not extend to the
  process itself dying, see [Correction](#correction-2026-07-31).
- One `run()` call mutates a single file (the one whose `source` it was given).

## Correction (2026-07-31)

The Consequences bullet above originally read:

> A crash or a runner exception mid-run still restores the file (`finally`), so a failed run never
> corrupts the project.

The "crash" half was not true, and the word "corrupts" promised a guarantee this strategy never had.

A `finally` block is interpreter-level bookkeeping: it runs only if the interpreter lives long enough
to run it. So it covers an *exception*, and by construction cannot cover the process ending, whether
that is a `SIGKILL` or a `TerminateProcess`, an OOM kill, or the machine losing power. That limit is
structural, not a bug in this code, and no refinement of the restore path removes it. Strategy (a)
mutates the user's real file, so whatever the process does not live to undo is left on disk.

Measured on 2026-07-31 against the implementation of the day: a hard kill delivered while the suite
was running left the target file holding the mutant, not the original. The write path also
truncates the target in place before writing, so the file passes through a zero-length state on both
the mutate and the restore step, **a window in which process death leaves an empty file rather than
either version of the source.**

What this ADR is therefore entitled to claim is the narrower guarantee: the engine restores the
file whenever it is still running to do so. How durable the on-disk write is against process death
is a property of the write path, tracked and changed independently of this decision, so read the
guarantee from the write path itself rather than from this record.

Only this record was wrong. The user-facing description in
[`docs/gdmutant-guide.md`](../gdmutant-guide.md#how-gdmutant-writes-to-your-files) already states the
limit correctly: a swap can persist through a hard kill, so commit or stash first, or pass
`--require-clean`. This correction brings the decision record in line with the guidance users
actually read.

## Correction (2026-08-01)

The correction above, written the same day as the code change it describes, said:

> The write path also truncates the target in place before writing, so the file passes through a
> zero-length state on both the mutate and the restore step, a window in which process death leaves
> an empty file rather than either version of the source.

That was true of the code at the moment it was written, and false of the code that landed a few
hours later the same day. `fix: never leave a source file truncated by an interrupted restore (#159)`
replaced the in-place write with the staged one `_write_source` in `gdmutant/engine/loop.py` still
uses: the new bytes go to a temporary file created beside the target, get flushed to disk, and are
then moved onto the target with `os.replace`, a single filesystem operation. The target itself is
never opened for truncation, so there is no zero-length window on the write path today, and no
"empty file" outcome for a kill landing during either the mutate or the restore write.

The narrower claim directly above this one still holds: a hard kill between the mutate write and the
restore write leaves the target holding the mutant, because the restore simply has not run yet. That
is a gap between two writes, not a truncation inside either one, and it is exactly the limit
`docs/gdmutant-guide.md#how-gdmutant-writes-to-your-files` already describes. Only the "truncates in place" /
"zero-length state" mechanism was wrong. Read `_write_source`'s own docstring for the current
mechanism rather than from this record, per the guidance the first correction already gave.
