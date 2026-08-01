---
type: decision
status: active
created: 2026-07-24
---

# A runner-agnostic adapter seam: GdUnit4 and GUT as peer JUnit adapters

## Status
Accepted, corrected 2026-08-01: the `GdUnit4Runner` crash-safety bullet rested on a single
observation, and described a `_result_from_report` that now exists. The decision itself stands. The
bullet is restated below and the original wording preserved under
[Correction](#correction-2026-08-01).

## Context
gdmutant runs a project's test suite once per mutant and reads the outcome through a single seam,
the engine's `Runner` protocol (`run(project_dir, timeout) -> SuiteResult`, plus the optional
`Preparable` warm-up). The engine is language- and framework-neutral (NF-3): it only ever holds a
`Runner`, never a concrete framework.

The first concrete adapter was `GdUnit4Runner` (JUnit-XML), and [ADR-0005](0005-exit-code-test-runner-convention.md)
added the framework-neutral exit-code `CommandRunner` for any harness that signals pass/fail via its
exit code. But GdUnit4 was implicitly *privileged*: it was the CLI's default and the only
first-class, per-test-detail path, and the docs described it as "the first test-runner adapter" as if
a second JUnit framework would be exotic.

[GUT (Godot Unit Test)](https://github.com/bitwes/Gut) is the other widely-used GDScript test
framework, and it *also* emits JUnit XML. Running GUT only through the coarse exit-code `CommandRunner`
throws away the per-test detail it can provide, and treats a first-class citizen as a second-class one.
A spike proved GUT works end-to-end against real Godot 4.7 + GUT v9.7.1, producing the exact same
per-mutant outcome as GdUnit4 on the corpus (18 mutants / 11 killed / 7 survivors, mutant-for-mutant).

The question this ADR settles: how should a second, and any future, JUnit-emitting framework fit,
without reshaping the engine or privileging one framework over another?

## Decision
Formalize the runner seam as a runner-agnostic adapter contract with GdUnit4 and GUT as peer
adapters, neither privileged in the engine:

- The contract is the `Runner` protocol (`engine/runner.py`). The engine knows only the contract.
  Adding a JUnit-emitting framework is one small adapter behind it, with no engine change.
- Two first-class JUnit adapters share one base, `_GodotJUnitRunner` (in the GDScript adapter):
  the cold-load `--import` warm-up (`Preparable`), the report-freshness guard (remove the old report,
  require this run's to reappear), timeout → `SuiteTimeout`, and JUnit parsing all live once. Each
  concrete adapter supplies only its own `command()` flags and its own crash-safety enforcement.
  `GdUnit4Runner` and `GutRunner` are siblings, not a base-and-special-case.
- The exit-code `CommandRunner` (ADR-0005) remains the documented fallback for the long tail, meaning
  any framework that emits *no* JUnit XML (a hand-rolled `SceneTree` harness, a bespoke CLI). So the
  seam is two first-class JUnit adapters plus one universal exit-code path.
- Deliberately not a plugin framework. Two concrete adapters plus a clear protocol, not a
  registry, entry points, or dynamic discovery. The seam earns its keep at two adapters, and a third
  JUnit framework is a copy of the pattern, cheap enough not to warrant abstraction machinery.

### The crash-safety clause (the contract property both adapters must uphold)
Every runner must surface a load or compile crash as a kill or an error, never a silent zero-test
pass. A runner that returned "0 tests, 0 failures" for a crash would mark the responsible mutant
SURVIVED, gdmutant's single worst failure mode (a wrong survivor report). Each adapter upholds it in
the way *its* framework fails:

- `CommandRunner`. A non-zero exit is a failure (killed), and a command that can't be executed at all
  raises (→ `error`).
- `GdUnit4Runner`. GdUnit4 loads every suite in the scanned directory during discovery, so a suite
  it cannot parse aborts the whole run and writes no report, caught by the report-reappear freshness
  guard (raises → `error`). Measured at n>1, not assumed. Its `_result_from_report`
  additionally raises on a report describing zero tests, so the contract does not rest on that
  measurement holding for every future GdUnit4. Both points are restated from a weaker original
  claim under [Correction](#correction-2026-08-01).
- `GutRunner`. GUT does not fail a run when a test file fails to compile or load: it skips
  that suite, runs the rest green, and exits 0 (confirmed live, see below). Two shapes surface as
  an execution error (raises → `error`), never a pass, plus a third, symmetric warning (the
  non-determinism canary) that closes the gap the drop-guard's stability assumption leaves open:
  1. `tests == 0`, the empty-report shape (a `<testsuites tests="0"/>` with no child, which the
     parser raises an *incidental* `ValueError` on, caught, or a child `<testsuite tests="0">`), or
     every suite skipped.
  2. A drop below the baseline test count. The first run (the engine's healthy baseline, run
     serially before any `--jobs` fan-out) fixes the expected count, and a later run with *fewer*
     tests is surfaced as `error` rather than a false survivor. This guard assumes deterministic,
     stable suite collection (as all mutation testing does). Under that assumption it is complete:
     any suite a mutant skips strictly drops the scalar total → error, never a silent pass. It does
     not cover variance, meaning a suite whose test count varies run-to-run, or that loads flakily.
     Such variance can (a) *mask* a real skip if another suite rises by the same amount (a residual
     false survivor the scalar total can't see), or (b) *false-error* on a benign dip. Stabilize a
     flaky suite before a run.
  3. A rise *above* the baseline test count → a run-level WARNING, the non-determinism canary. A
     legitimate mutant can never raise the collected test count *above* the healthy baseline, since
     a mutation cannot add test files, so a later run reporting more tests than the baseline
     deterministically proves the baseline undercounted: suite collection is non-deterministic, the
     one condition (2)'s stability assumption excludes. This is surfaced as a warning (via the
     runner's `run_warning`, on the same stderr surface as the "all mutants survived" warning) and
     never as an error, because flipping the mutant to error would false-error on benign flakiness.
     It is the canary that makes the otherwise-unobservable variance-masking case observable: a
     silently-masked skip is a false survivor that can never be *seen*, so the earlier trigger
     "widen if variance-masking is observed in practice" was anchored on something unobservable. The
     canary closes that loop. Trigger to widen to per-suite baseline tracking: when the
     non-determinism canary (`tests > baseline`) fires, not on assumption, and not on the old
     unobservable condition. Until it fires, the scalar-total guard (1)+(2) stands, and per-suite
     tracking stays correctly deferred.
  (1) and (2) are directly tested (unit) and pinned by the live n>1 probe below. (3) is unit-tested
  (a run above baseline warns and does not error).

#### Proving GUT's clause at n>1 (not just n=1)
The `tests == 0` guard only bites if a compile crash actually *zeroes* the run. The bundled corpus has
a single TurnOrder-referencing GUT suite, so breaking `turn_order.gd` breaks the only suite, and the
guard is proven there only at n=1. The real-world risk is a multi-file suite: if a mutant
breaks just the file(s) that reference the mutated source and GUT skips the broken file and runs the
rest, the report carries the healthy files' green tests (`tests > 0, failures = 0`) → a pass →
SURVIVED, a false survivor straight *through* the `tests == 0` guard.

Rather than widen the guard on assumption, a live probe settled it empirically
(`tests/test_selftest_live.py::test_gut_crash_safety_never_reports_a_false_survivor_at_n_gt_1`): a
second, independent GUT suite (`corpus/gut_test/test_independent_gut.gd`) that compiles and passes on
its own is added, a healthy baseline is run, `turn_order.gd` is made uncompilable, and the SAME GUT
runner is run again. The invariant it asserts is the real one, never a passing `SuiteResult`. The
mutant run must come back a kill *or* an error. It runs against real Godot + GUT in the `selftest-gut`
CI leg and prints which branch GUT took.

> CI finding (real GUT v9.7.1, Godot 4.7): skip-and-continue. With `turn_order.gd`
> uncompilable, GUT skipped its referencing suite, ran the independent suite green, and reported
> `tests=2, failures=0` (a would-be false survivor). So the `tests == 0` guard is not sufficient
> at n>1, and the guard was widened to also error on a drop below the baseline test count
> (implemented in `GutRunner._result_from_report`, and the probe now passes because that drop is
> caught). The failing case was caught here, in gdmutant's own gate, not in an adopter's report,
> exactly what the probe exists for. (A separate bug surfaced alongside it and was fixed: GUT does
> not create the `-gjunit_xml_file` parent directory, so the base runner now `mkdir -p`s it before
> every run.)

## Consequences
- GUT is a first-class runner (`--runner gut`), with the same per-test JUnit detail as GdUnit4 and
  no new engine code. The live self-test pins GUT to the *same* 18/11/7 corpus outcome as GdUnit4,
  and mutant-for-mutant agreement is the proof the seam is genuinely framework-agnostic rather than
  GdUnit4-shaped.
- Any future JUnit-emitting framework is first-class by adding one small adapter subclassing
  `_GodotJUnitRunner`: its `command()` and its `_result_from_report`. Both are abstract, so a new
  adapter has to state how its own framework fails rather than inheriting a permissive default (see
  [Correction](#correction-2026-08-01)).
- Per-runner defaults. `--report-path` defaults per runner (gdunit4 `reports/report_1/results.xml`,
  gut `reports/gut_results.xml`), resolved in the CLI, and `--tests` maps to GdUnit4's `-a` or GUT's
  `-gdir`.
- The exit-code path is unchanged and stays the documented answer for frameworks without JUnit
  output. It is *complementary* to the two JUnit adapters, not superseded by them.
- Slightly more indirection in the GDScript adapter (a shared base plus two subclasses instead of one
  class). Accepted: it removes ~60 lines of duplicated warm-up and guard logic and makes the shared
  contract, crash-safety included, explicit and enforced identically for both.

## Correction (2026-08-01)

The `GdUnit4Runner` bullet in the crash-safety clause originally read:

> `GdUnit4Runner`. A crash writes no report, caught by the report-reappear freshness guard
> (raises → `error`). Its `_result_from_report` is the base's plain parse, so no override is needed.

What it describes is right. The evidence behind it was not strong enough to carry it, and the last
sentence is now out of date.

"A crash writes no report" was observed once, against a corpus holding a single GdUnit4 suite. On a
one-suite project, breaking the file under test breaks the only suite, so there is nothing left for
the framework to run and report either way. That is the same n=1 evidence GUT passed before its own
live probe, and GUT turned out to skip the broken suite, run the rest green, and exit 0, which is a
false survivor. GdUnit4 is the default runner, so the weaker claim was sitting on the path most
people take.

### Proving GdUnit4's clause at n>1 (not just n=1)

The corpus now carries a second GdUnit4 suite that does not reference `TurnOrder`,
`corpus/test/test_independent.gd`, the exact peer of the GUT one. It compiles and passes on its own,
so breaking `turn_order.gd` leaves one suite broken and one healthy, which is the shape a single-suite
corpus could never produce.
`tests/test_selftest_live.py::test_gdunit4_crash_safety_never_reports_a_false_survivor_at_n_gt_1`
drives the same probe GUT gets: run a healthy baseline, make `turn_order.gd` uncompilable, run the
same runner again, and assert the result is never a passing `SuiteResult`.

> Measured (real GdUnit4 v6.1.3, Godot 4.7, 2026-08-01): abort at discovery. Healthy baseline first,
> for comparison: both suites loaded, 5 tests, 0 failures, exit 0, report written. Then with
> `turn_order.gd` uncompilable, GdUnit4 printed "Script errors were detected during test discovery!",
> exited 105, and wrote no report at all. The healthy suite never ran. GdUnit4 loads every suite in
> the scanned directory before running any of them, so the one it cannot parse stops the whole run.
> It does not skip and continue the way GUT does. The report-reappear guard is what catches this,
> and no drop-below-baseline guard is needed here, because there is no green report for a drop to be
> measured against.

So the claim now rests on n>1, and the probe is what keeps it there. If a future GdUnit4 starts
skipping broken suites, that probe fails in gdmutant's own gate, which is when the guard gets
widened. That is the same trigger `GutRunner`'s canary uses.

The same run measured GdUnit4's empty-discovery behaviour, which turns out to be a different failure
with the same symptom: pointed at a directory holding no GdUnit4 suites, or at one that does not
exist, GdUnit4 exits 0, writes no report, and prints "No test cases found, abort test run!".
`GdUnit4Runner` now reads that marker and reports it as test discovery rather than as a crash, so a
wrong `--tests` path names the flag that fixes it instead of sending someone to debug a crash that is
not happening. Matching on the marker is fail-safe: if a future GdUnit4 rewords it, the run still
fails with the generic no-report error, only diagnosed less precisely.

### What changed in the code

`GdUnit4Runner` now has its own `_result_from_report`, which raises on a report describing zero tests
instead of returning `SuiteResult(0, 0, 0)`, whose `failed` is False and would therefore read as a
clean pass. No GdUnit4 version measured here writes such a report. The guard exists so the contract
does not depend on that measurement holding, which is exactly the assumption that made the GUT false
survivor possible. The base's parse-only default is gone with it. `_result_from_report` is abstract
now, so an adapter that forgets to say how its framework fails raises `NotImplementedError` the first
time it runs, rather than quietly returning a pass for a run that never happened.

The zero-test check also moved up into the engine. A baseline that ran no tests is a property of a
baseline, not of a framework, and it is reachable under any runner pointed at the wrong directory.
`engine/loop.py` now refuses a zero-test baseline outright, and marks a zero-test mutant run as an
error rather than a survivor, so all three runners are covered instead of only the one that happened
to have the check. The adapters keep their own zero-test guards, and that is not duplication: the
engine states the condition, an adapter can state the cause and the cure, and whichever raises first
wins with the better message.

One limit of the engine backstop is worth stating plainly, because it is not a gap that can be
closed. The backstop reads test counts, and `CommandRunner` has none. It reports `tests=1` for any
exit-0 run, because an exit code cannot say how many tests ran, so a harness that discovers nothing
and exits 0 is indistinguishable from one that passed. A command used with `--runner command` has to
exit non-zero when it finds no tests. Nothing downstream can recover that distinction once the exit
code is 0. The two JUnit adapters have real counts and are fully covered.
