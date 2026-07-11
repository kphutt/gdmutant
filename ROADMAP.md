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

## Later (deferred — do not build now)
- Coverage-gated mutant selection (the #1 speedup; GDScript coverage tooling is immature).
- HTML report output; incremental / diff-scoped (per-PR) mode.
- Optional LLM-semantic mutants (plausible-bug mode) — kept *out* of the deterministic path.
- A second-language adapter (TypeScript delegates to Stryker, or is skipped).
- Publish to PyPI (`pipx install gdmutant`).
