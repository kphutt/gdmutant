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
- **`GutRunner`** — a crash writes an **empty** `<testsuites tests="0" …/>` and exits **0**. Left
  alone that either parses to a clean zero-test pass or raises an *incidental* `ValueError` deep in
  the parser. `GutRunner` makes it **explicit**: `tests == 0` (by any empty-report shape) is an
  execution error (raises → `error`). This is the one GUT-specific hardening, and it is directly
  tested as a false-survivor regression guard.

#### Proving GUT's clause at n>1 (not just n=1)
The `tests == 0` guard only bites if a compile crash actually *zeroes* the run. The bundled corpus has
a single TurnOrder-referencing GUT suite, so breaking `turn_order.gd` breaks the only suite — the
guard is proven there **only at n=1**. The real-world risk is a **multi-file** suite: if a mutant
breaks just the file(s) that reference the mutated source and GUT **skips the broken file and runs the
rest**, the report carries the healthy files' green tests (`tests > 0, failures = 0`) → a pass →
SURVIVED — a false survivor straight *through* the `tests == 0` guard.

Rather than widen the guard on assumption, a **live probe** settles it empirically
(`tests/test_selftest_live.py::test_gut_crash_safety_never_reports_a_false_survivor_at_n_gt_1`): a
second, independent GUT suite (`corpus/gut_test/test_independent_gut.gd`) that compiles and passes on
its own is added, `turn_order.gd` is made uncompilable, and the GUT runner is driven directly. The
invariant it asserts is the real one — **never a passing `SuiteResult`**; the run must come back a
kill *or* an error. The probe runs against real Godot + GUT in the `selftest-gut` CI leg and prints
which branch GUT actually took (abort-all → the empty `tests == 0` report → the guard fires;
skip-and-continue → healthy tests → a false survivor; run-and-fail → the broken suite errors at
runtime → killed). If the probe passes, the `tests == 0` guard is sufficient at n>1 as-is; if it reds
(GUT skip-and-continues), the guard is widened then (e.g. a per-run test-count-drop check) — either
way the failing case is caught here, in gdmutant's own gate, not in an adopter's report.

> **CI finding (real GUT v9.7.1):** recorded from the `selftest-gut` leg once it has run the probe —
> which of {abort-all, skip-and-continue, run-and-fail} GUT takes, and whether the guard was widened.

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
