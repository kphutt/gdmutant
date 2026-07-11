# Roadmap

<!-- Prioritized big rocks — the backlog. Mark done items ~~struck~~ ✅. -->

## Done — v0.1 (mutates real GDScript + reports survivors)
- ~~Bootstrap: repo hardening + Python CI + package skeleton + docs spine~~ ✅
- ~~`DESIGN.md` — the reviewed design gate~~ ✅
- ~~Engine loop: select → mutate → run → tally → mutation score~~ ✅
- ~~Operator catalog: boolean, comparison, arithmetic, constant, numeric-literal~~ ✅
  *(statement deletion is the one remaining FG-2.1 operator — see below)*
- ~~GDScript adapter: gdtoolkit AST → mutate → NF-5 re-parse guard → GdUnit4 runner~~ ✅
- ~~Bundled `corpus/` fixture + end-to-end~~ ✅ · ~~Stryker `mutation-testing-elements` JSON report~~ ✅
- ~~`gdmutant run` CLI (standalone, no AI required)~~ ✅

## Remaining to finish v0.1 → public
- **Live CI Godot validation** — a `setup-godot` job that installs Godot + the GdUnit4 addon and
  validates `GdUnit4Runner`'s exact CLI + report path against real output (currently unit-tested with
  the subprocess mocked).
- **Statement-deletion operator** — the last FG-2.1 mutation; structural (replace a statement with
  `pass`), so it needs AST statement-node handling rather than a token swap.
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
- **Multi-file / directory targets** — mutate a set of files (or a directory) in one run with an
  aggregate mutation score (today: one `.gd` file per invocation), plus a helper to merge the
  per-file Stryker reports. Running the CLI once per file already works (mutation is in-place in the
  real project tree, so cross-file class references resolve), but there's no aggregate score.
- **More operators for real code** — unary `not`, modulo `%`, and compound assignment
  (`+=`/`-=`/`*=`/`/=`, which gdtoolkit tokenizes atomically) all appear in real logic the current
  token-swap catalog can't mutate.

## Later (deferred — do not build now)
- Coverage-gated mutant selection (the #1 speedup; GDScript coverage tooling is immature).
- HTML report output; incremental / diff-scoped (per-PR) mode.
- Optional LLM-semantic mutants (plausible-bug mode) — kept *out* of the deterministic path.
- A second-language adapter (TypeScript delegates to Stryker, or is skipped).
- Publish to PyPI (`pipx install gdmutant`).
