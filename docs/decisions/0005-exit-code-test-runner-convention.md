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

### Harness-authoring caveat: the load-failure exit-0 trap ([ticket])

The exit-code convention has one sharp edge for a **`godot --headless --script <harness>`** harness:
Godot exits **0 when the entry script itself fails to load** (a parse/compile error), not just on a
clean pass. So a harness that `preload`s the file-under-test will, when a mutant makes that file
uncompilable, fail to compile *itself* → Godot exits 0 → the CommandRunner reads a **false PASS** and
the broken mutant wrongly **survives**. Verified against Godot 4.7.

This is largely theoretical for gdmutant because generation-time guards (NF-5 gdtoolkit re-parse +
the return-path guard, `docs/decisions/0007`) block most uncompilable mutants before they run, and
the external per-mutant timeout ([ticket]) catches hangs. But a harness author must not rely on that.
Guidance for a hand-rolled harness, in order of reliability:

1. **Don't couple to the target at compile time.** `load()` the file-under-test at runtime and
   **gate on `GDScript.can_instantiate()`** before calling into it, quitting non-zero if it's false.
   `load()` returns a *non-null broken* `GDScript` on a compile error (so `if T == null` never
   fires), and *directly calling* such a script **hangs** the process — `can_instantiate()` is a
   gate that never hangs. **Caveat:** it isn't a pure "did it compile" test — a *cleanly-compiled*
   `@abstract` class (Godot 4.5+) also reports `can_instantiate() == false`. So the gate is exact
   for a **concrete** target (as `corpus/harness/run_tests.gd`, the reference example, has); a
   harness whose file-under-test is `@abstract` should instead load and gate on a **concrete
   subclass** it can instantiate, or verify the load some other way that doesn't call into the
   possibly-broken script.
2. **Never trust the exit code alone.** Additionally print a success **sentinel** (e.g.
   `HARNESS_PASSED`) as the harness's last line and treat "sentinel absent" as failure — it survives
   even the exit-0 load-failure class. (gdmutant's `CommandRunner` is exit-code-only today; a
   sentinel-aware runner is possible future work behind the same `Runner` seam.)
3. **An in-process watchdog does NOT help.** A one-shot timer / `_process` deadline cannot interrupt
   a *synchronous* mutant (a compiling `while true`), because it starves the single main thread. The
   real hang defense is the **external** per-mutant subprocess timeout, which gdmutant already has.
