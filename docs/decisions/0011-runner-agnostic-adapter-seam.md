---
type: decision
status: active
created: 2026-07-24
---

# A runner-agnostic adapter seam: GdUnit4 and GUT as peer JUnit adapters

## Status
Accepted

## Context
gdmutant runs a project's test suite once per mutant and reads the outcome through a single seam —
the engine's `Runner` protocol (`run(project_dir, timeout) -> SuiteResult`, plus the optional
`Preparable` warm-up). The engine is language- and framework-neutral (NF-3): it only ever holds a
`Runner`, never a concrete framework.

The first concrete adapter was `GdUnit4Runner` (JUnit-XML), and [ADR-0005](0005-exit-code-test-runner-convention.md)
added the framework-neutral exit-code `CommandRunner` for any harness that signals pass/fail via its
exit code. But GdUnit4 was implicitly *privileged* — it was the CLI's default and the only
first-class, per-test-detail path, and the docs described it as "the first test-runner adapter" as if
a second JUnit framework would be exotic.

[GUT (Godot Unit Test)](https://github.com/bitwes/Gut) is the other widely-used GDScript test
framework, and it *also* emits JUnit XML. Running GUT only through the coarse exit-code `CommandRunner`
throws away the per-test detail it can provide, and treats a first-class citizen as a second-class one.
A spike proved GUT works end-to-end against real Godot 4.7 + GUT v9.7.1, producing the **exact same**
per-mutant outcome as GdUnit4 on the corpus (18 mutants / 11 killed / 7 survivors, mutant-for-mutant).

The question this ADR settles: how should a second — and any future — JUnit-emitting framework fit,
without reshaping the engine or privileging one framework over another?

## Decision
Formalize the runner seam as a **runner-agnostic adapter contract** with GdUnit4 and GUT as **peer
adapters**, neither privileged in the engine:

- **The contract is the `Runner` protocol** (`engine/runner.py`). The engine knows only the contract.
  Adding a JUnit-emitting framework is one small adapter behind it — no engine change.
- **Two first-class JUnit adapters share one base**, `_GodotJUnitRunner` (in the GDScript adapter):
  the cold-load `--import` warm-up (`Preparable`), the report-freshness guard (remove the old report,
  require this run's to reappear), timeout → `SuiteTimeout`, and JUnit parsing all live once. Each
  concrete adapter supplies only its own `command()` flags and its own crash-safety enforcement.
  `GdUnit4Runner` and `GutRunner` are siblings, not a base-and-special-case.
- **The exit-code `CommandRunner` (ADR-0005) remains the documented fallback** for the long tail — any
  framework that emits *no* JUnit XML (a hand-rolled `SceneTree` harness, a bespoke CLI). So the seam
  is: *two first-class JUnit adapters + one universal exit-code path.*
- **Deliberately not a plugin framework.** Two concrete adapters plus a clear protocol — not a
  registry, entry points, or dynamic discovery. The seam earns its keep at two adapters; a third
  JUnit framework is a copy of the pattern, cheap enough not to warrant abstraction machinery.

### The crash-safety clause (the contract property both adapters must uphold)
Every runner must surface a load/compile crash as a **kill or an error — never a silent zero-test
pass.** A runner that returned "0 tests, 0 failures" for a crash would mark the responsible mutant
SURVIVED — gdmutant's single worst failure mode (a wrong survivor report). Each adapter upholds it in
the way *its* framework fails:

- **`CommandRunner`** — a non-zero exit is a failure (killed); a command that can't be executed at all
  raises (→ `error`).
- **`GdUnit4Runner`** — a crash writes **no** report, caught by the report-reappear freshness guard
  (raises → `error`). Its `_result_from_report` is the base's plain parse; no override needed.
- **`GutRunner`** — GUT does **not** fail a run when a test file fails to compile/load: it **skips**
  that suite, runs the rest green, and exits **0** (confirmed live — see below). Two shapes surface as
  an execution **error** (raises → `error`), never a pass — plus a third, symmetric **warning** (the
  non-determinism canary) that closes the gap the drop-guard's stability assumption leaves open:
  1. **`tests == 0`** — the empty-report shape (a `<testsuites tests="0"/>` with no child, which the
     parser raises an *incidental* `ValueError` on — caught — or a child `<testsuite tests="0">`), or
     every suite skipped.
  2. **a drop below the baseline test count** — the first run (the engine's healthy baseline, run
     serially before any `--jobs` fan-out) fixes the expected count; a later run with *fewer* tests
     is surfaced as `error` rather than a false survivor. **This guard assumes deterministic, stable
     suite collection** (as all mutation testing does). Under that assumption it is complete: any
     suite a mutant skips strictly drops the scalar total → error, never a silent pass. It does
     **not** cover variance — a suite whose test count varies run-to-run, or that loads flakily.
     Such variance can (a) *mask* a real skip if another suite rises by the same amount (a residual
     false survivor the scalar total can't see), or (b) *false-error* on a benign dip. Stabilize a
     flaky suite before a run.
  3. **a rise *above* the baseline test count → a run-level WARNING (the non-determinism canary).** A
     legitimate mutant can never raise the collected test count *above* the healthy baseline — a
     mutation cannot add test files — so a later run reporting **more** tests than the baseline
     deterministically proves the baseline *undercounted*: suite collection is non-deterministic, the
     one condition (2)'s stability assumption excludes. This is surfaced as a **warning** (via the
     runner's `run_warning`, on the same stderr surface as the "all mutants survived" warning),
     **never** an error — flipping the mutant to error would false-error on benign flakiness. It is
     the **canary that makes the otherwise-unobservable variance-masking case observable**: a
     silently-masked skip is a false survivor that can never be *seen*, so the earlier trigger
     "widen if variance-masking is observed in practice" was anchored on something unobservable. The
     canary closes that loop. **Trigger to widen to per-suite baseline tracking:** when the
     non-determinism canary (`tests > baseline`) fires — not on assumption, and not on the old
     unobservable condition. Until it fires, the scalar-total guard (1)+(2) stands; per-suite
     tracking stays correctly deferred.
  (1) and (2) are directly tested (unit) and pinned by the live n>1 probe below; (3) is unit-tested
  (a run above baseline warns and does not error).

#### Proving GUT's clause at n>1 (not just n=1)
The `tests == 0` guard only bites if a compile crash actually *zeroes* the run. The bundled corpus has
a single TurnOrder-referencing GUT suite, so breaking `turn_order.gd` breaks the only suite — the
guard is proven there **only at n=1**. The real-world risk is a **multi-file** suite: if a mutant
breaks just the file(s) that reference the mutated source and GUT **skips the broken file and runs the
rest**, the report carries the healthy files' green tests (`tests > 0, failures = 0`) → a pass →
SURVIVED — a false survivor straight *through* the `tests == 0` guard.

Rather than widen the guard on assumption, a **live probe** settled it empirically
(`tests/test_selftest_live.py::test_gut_crash_safety_never_reports_a_false_survivor_at_n_gt_1`): a
second, independent GUT suite (`corpus/gut_test/test_independent_gut.gd`) that compiles and passes on
its own is added, a healthy baseline is run, `turn_order.gd` is made uncompilable, and the SAME GUT
runner is run again. The invariant it asserts is the real one — **never a passing `SuiteResult`**; the
mutant run must come back a kill *or* an error. It runs against real Godot + GUT in the `selftest-gut`
CI leg and prints which branch GUT took.

> **CI finding (real GUT v9.7.1, Godot 4.7):** **skip-and-continue** — with `turn_order.gd`
> uncompilable, GUT skipped its referencing suite, ran the independent suite green, and reported
> `tests=2, failures=0` (a would-be false survivor). So the `tests == 0` guard is **not** sufficient
> at n>1, and the guard was **widened** to also error on a **drop below the baseline test count**
> (implemented in `GutRunner._result_from_report`; the probe now passes because that drop is caught).
> The failing case was caught here, in gdmutant's own gate, not in an adopter's report — exactly what
> the probe exists for. (A separate bug surfaced alongside it and was fixed: GUT does not create the
> `-gjunit_xml_file` parent directory, so the base runner now `mkdir -p`s it before every run.)

## Consequences
- **GUT is a first-class runner** (`--runner gut`), with the same per-test JUnit detail as GdUnit4 and
  no new engine code. The live self-test pins GUT to the *same* 18/11/7 corpus outcome as GdUnit4 —
  mutant-for-mutant agreement is the proof the seam is genuinely framework-agnostic, not GdUnit4-shaped.
- **Any future JUnit-emitting framework is first-class by adding one small adapter** subclassing
  `_GodotJUnitRunner` — its `command()` and (if its crash mode differs) its `_result_from_report`.
- **Per-runner defaults.** `--report-path` defaults per runner (gdunit4 `reports/report_1/results.xml`,
  gut `reports/gut_results.xml`), resolved in the CLI; `--tests` maps to GdUnit4's `-a` / GUT's `-gdir`.
- **The exit-code path is unchanged** and stays the documented answer for frameworks without JUnit
  output — it is *complementary* to the two JUnit adapters, not superseded by them.
- **Slightly more indirection** in the GDScript adapter (a shared base + two subclasses instead of one
  class). Accepted: it removes ~60 lines of duplicated warm-up/guard logic and makes the shared
  contract — including crash-safety — explicit and enforced identically for both.
