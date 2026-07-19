# Apply mutants on disk, one at a time, with restore

## Status
Accepted

## Context
The runner runs the target project's **real** test suite (`godot --headless` + GdUnit4), which loads
source from disk (`res://` files). So a mutant must be materialized on disk for the suite to
exercise it. Options:

- **(a) Write the mutated file → run the suite → restore the original.**
- **(b) Mutant schemata / "switching"** — bake all mutants into one instrumented copy and toggle
  each via a runtime switch (the PIT optimization). Fast (parse/instrument once) but complex and
  language-specific.
- **(c) Copy the whole project per mutant** — safe isolation, but slow and wasteful.

## Decision
Use **(a): on disk, one mutant at a time, with the original restored in a `finally`.** For each
mutant the engine writes the mutated source to the file, runs the suite, and restores the original —
even if the runner raises. Each mutant is applied in isolation (FG-1.2), and the project is never
left mutated. Invalid mutants (NF-5) are classified without ever touching disk. Simple and correct
for v0.1's full-suite-per-mutant model.

## Consequences
- One file write + one full suite run per mutant. Booting Godot per mutant is slow — the perf path
  is coverage-gated selection and, later, **schemata (b)**, both deferred to Tier B (`DESIGN.md`
  §5). This decision does not preclude adopting (b) later; it's the simple correct baseline.
- A crash or a runner exception mid-run still restores the file (`finally`), so a failed run never
  corrupts the project.
- One `run()` call mutates a single file (the one whose `source` it was given).
