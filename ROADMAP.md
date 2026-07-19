# Roadmap

<!-- Prioritized big rocks — the backlog. Mark done items ~~struck~~ ✅. -->

## Done — v0.1 (mutates real GDScript + reports survivors)
- ~~Bootstrap: repo hardening + Python CI + package skeleton + docs spine~~ ✅
- ~~`DESIGN.md` — the reviewed design gate~~ ✅
- ~~Engine loop: select → mutate → run → tally → mutation score~~ ✅
- ~~Operator catalog: boolean, comparison, arithmetic, constant, numeric-literal, statement-deletion~~ ✅
- ~~GDScript adapter: gdtoolkit AST → mutate → NF-5 re-parse guard → GdUnit4 runner~~ ✅
- ~~Bundled `corpus/` fixture + end-to-end~~ ✅ · ~~Stryker `mutation-testing-elements` JSON report~~ ✅
- ~~`gdmutant run` CLI (standalone, no AI required)~~ ✅

## Remaining to finish v0.1 → public
- ~~**Live CI Godot validation** — a `setup-godot` job that installs Godot + the GdUnit4 addon and
  validates both runner paths against real output~~ ✅ — `tests/test_selftest_live.py` +
  `scripts/install-gdunit4.sh` + the `selftest-godot` CI job drive the shipped CLI against **real
  Godot**, pinned to exact per-mutant outcomes. Caught two real runner bugs (`--ignoreHeadlessMode`,
  relative-project path). *Follow-up: flip the job to a required status check after a short soak.*
- ~~**Statement-deletion operator** — the last FG-2.1 mutation; structural (replace a statement with
  `pass`)~~ ✅ — in the GDScript adapter with a generation-time return-path guard so a deletion Godot
  can't compile is never emitted (`docs/decisions/0007`). The FG-2.1 catalog is now complete.
- Then flip the repo **public** (private now; never launch empty).

## Known debt (pre-public cleanup)
- **NF-3 — the engine hard-imports the GDScript adapter.** `engine/loop.py` imports
  `apply_mutant`/`generate_mutants` from `adapters/gdscript` directly, so the engine is not yet the
  language-neutral core DESIGN.md NF-3 requires. Fix: inject the adapter into `run()` the way
  `runner` and `catalog` already are (a small `Adapter` protocol or two callables). Mechanical
  (~20 lines + tests); no functional impact today, since GDScript is the only adapter.

## Real-project adoption (beyond the bundled corpus)
*What a real Godot project needs before it can point gdmutant at its own systems.*
- **Framework-agnostic test runner** — a second `Runner` (the `Runner` protocol already exists for
  this) that runs a project's *own* headless test script and reads a simple stdout/exit-code
  convention, so gdmutant works against projects that don't use the GdUnit4 addon. Needs an ADR for
  the convention. This is the main thing gating adoption by projects with a hand-rolled test harness.
- ~~**Multi-file / directory targets** — mutate a set of files (or a directory) in one run with an
  aggregate mutation score, plus one merged Stryker report~~ ✅ — `gdmutant run <dir>` (or several
  files/dirs) expands to every `.gd` under the target (recursively, skipping `addons/` and dot-dirs),
  runs the baseline once, and emits a per-file breakdown, one aggregate score, and one merged
  JSON/HTML report (`engine.run_paths`, `report.stryker_report_multi`, `cli.run_mutation_paths`). This
  was the last "real-project adoption" gap — point it at your source directory now, not one file at a
  time. GdUnit4 / GUT test suites under the target are skipped by default (by `test/` dir, name
  affix, or `extends GdUnitTestSuite`/`GutTest`), matching StrykerJS/cargo-mutants; a `--exclude`
  glob (repeatable, also a `.gdmutant.toml` `exclude` list) skips anything else — generated or
  vendored code — the escape hatch every mutation tool pairs with its default ([ticket]).

## Later (deferred — do not build now)
- Method-body mutation coverage in the dogfood — mutmut 3 mutates only module-level functions, not
  class methods (see `docs/mutation-testing.md`); evaluate cosmic-ray (or a config/upstream fix) to
  cover method bodies too.
- Coverage-gated mutant selection (the #1 speedup; GDScript coverage tooling is immature).
- HTML report output; incremental / diff-scoped (per-PR) mode.
- Optional LLM-semantic mutants (plausible-bug mode) — kept *out* of the deterministic path.
- A second-language adapter (TypeScript delegates to Stryker, or is skipped).
- Publish to PyPI (`pipx install gdmutant`).
