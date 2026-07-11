# A framework-agnostic test runner via the exit-code convention

## Status
Accepted

## Context
gdmutant's only runner was `GdUnit4Runner`, which requires the GdUnit4 addon (`res://addons/gdUnit4/`)
and reads GdUnit4's JUnit-XML output. That excludes every Godot project with a **hand-rolled test
harness** — including the driver project, `project-rampart`, whose harness is
`godot --headless --script res://tests/run_tests.gd`: it prints its own PASS/FAIL to stdout and exits
non-zero on failure, with no JUnit XML. So gdmutant could not run against it at all — the single
biggest blocker to real-world adoption.

The engine already drives a `Runner` protocol (`run(project_dir) -> SuiteResult`), so a second runner
is the natural fit. The question is what convention it reads. Options:

- **(a) Exit code.** Exit `0` = the suite passed; any non-zero = it failed. Universal — every test
  harness and CI system already honours it, and rampart's does today with no changes.
- **(b) Parse stdout for a project-specific PASS/FAIL or count.** Richer (per-test counts), but every
  project prints differently, so it needs per-project configuration/regex and is brittle.
- **(c) Require another standard report format (TAP, a second XML dialect).** Less brittle than (b),
  but still forces the target project to adopt a format it may not emit.

## Decision
Add **`CommandRunner`** (in `engine/runner.py` — it only shells out and reads the exit code, so it is
language- and framework-neutral, unlike the GDScript-specific `GdUnit4Runner`). It runs an arbitrary
command with `cwd = project_dir` and maps **(a) the exit code** to a `SuiteResult`:

- exit `0` → `SuiteResult(tests=1, failures=0, errors=0)` (passed),
- non-zero → `SuiteResult(tests=1, failures=1, errors=0)` (failed → the mutant is killed).

The CLI selects it with `--runner command --command "<the test command>"` (default stays
`--runner gdunit4`). The command string is `shlex`-split.

## Consequences
- **gdmutant now runs against any Godot project (or any project at all) whose tests signal via the
  exit code** — rampart included, with zero changes to its harness.
- **Coarser than JUnit XML.** The exit code can't separate a *test failure* from the harness itself
  *erroring* (both are non-zero), so a mutant that makes the run crash counts as *killed*. This is an
  accepted trade-off:
  - NF-5 still holds where it can: a mutant that doesn't **parse** is filtered before it ever runs
    (classified invalid, never killed).
  - A mutant that can't be **executed at all** (e.g. a missing `godot` binary) raises, and the
    engine tallies that as `error`, not killed — the same as `GdUnit4Runner`.
  - Per-test counts aren't available, so the runner reports the suite as a single pass/fail unit.
- Projects wanting the finer killed/invalid/errored distinction keep using `GdUnit4Runner` (JUnit
  XML). A future runner could read a richer convention (TAP, a stdout count) without changing the
  engine — the `Runner` protocol is the seam.
