---
type: record
status: active
created: 2026-07-18
---

# Changelog

All notable changes to gdmutant are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it tags its first release.

## [0.1.0] — unreleased

gdmutant mutates real GDScript and reports survivors end-to-end via the standalone `gdmutant run`
CLI. `0.1.0` is the current in-development version; the first git tag is still pending.

### Added

- AST-based mutation of GDScript via [gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit),
  with a re-parse validity guard so invalid mutants are never run.
- Operators: comparison, boolean, arithmetic, constant, numeric-literal, compound assignment,
  modulo, unary-not, and statement-deletion.
- Framework-agnostic test runners over one shared runner contract (a runner-agnostic adapter seam):
  **GdUnit4** and **GUT** as first-class peer JUnit-XML adapters (`--runner gdunit4` / `--runner gut`,
  neither privileged in the engine), plus an exit-code runner (`--runner command`) for any headless
  harness without JUnit output — no addon required. Every runner upholds a crash-safety guarantee: a
  load/compile crash surfaces as a kill or error, never a silent zero-test pass (GUT's empty-report
  case is caught explicitly as `tests == 0` → error).
- Multi-file and directory targets: mutate several files or a whole directory in one pass with a
  per-file breakdown and one aggregate mutation score.
- `--jobs N` runs N mutants in parallel, each on its own copy of the project so in-place mutation
  can't collide — same verdicts as a serial run (process isolation; the per-mutant timeout is scaled
  by N so contention can't cause a false timeout), just faster (measured ~3× at `--jobs 4` on a real
  GdUnit4 module).
- Test suites are skipped by default on directory targets (by `test/`/`tests/` folder, `test_*.gd` /
  `*_test.gd` / `*Test.gd` name, or `extends GdUnitTestSuite` / `GutTest`), with an `--exclude` glob
  (and a `.gdmutant.toml` `exclude` list) to skip anything else.
- Reports: a console survivor summary that explains each gap (what's untested, why it matters, where
  to start a test), the
  [`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) JSON
  schema (`--json`), and a self-contained HTML report (`--html`).
- `.gdmutant.toml` for persisted per-project flags; `--dry-run` to list mutants without running
  Godot.
- Live self-test against real Godot in CI, pinning both runner paths to exact per-mutant outcomes.

### Safety

- Mutations are applied in place and restored after each mutant and on exit; gdmutant warns on
  uncommitted changes and `--require-clean` makes that a hard stop.
