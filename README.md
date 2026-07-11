# gdmutant

> **`gdmutant` is a provisional codename**, not yet cleared for public use. It lives in one
> place: this README + the repo name.

**A mutation-testing tool — the first *usable* one for GDScript/Godot, built to be language-agnostic.** Point it at a
codebase with a test suite; it mutates the source (flip `>`↔`>=`, `and`↔`or`, drop a `return`, …), reruns
the tests per mutant, and reports **survivors** — lines a bug could live on and no test would catch. Coverage
says a line *ran*; mutation says a bug there would be *caught*. That gap is the product.

## Why this exists (the opening)
- **No *usable* mutation tester exists for GDScript.** [Stryker](https://stryker-mutator.io/) does JS/TS,
  C#, Scala; mutmut does Python; PIT does Java. GDScript has only a **dormant 0-star unlicensed POC**
  ([hanse7962/GodotMutationTesting](https://github.com/hanse7962/GodotMutationTesting) — a few weeks of work
  in Apr–May 2026, no README, no license, undiscoverable), so the space for a real, adopted, documented tool
  is open. And the AST work is nearly free now that
  [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit) ships a real GDScript parser.
- **AI just opened the demand.** When an AI writes the tests, coverage is *especially* a lie (models write
  tests that pin the code they just wrote). Mutation is one of the few **executable, model-independent**
  signals that a test actually bites. So under-tested ecosystems like game-dev suddenly need this.
- **Extracted from real use, not built speculatively.** The driver is `project-rampart` (a Godot roguelike)
  needing to trust its two hardest systems (turn scheduler + procgen connectivity). This tool gets *extracted
  from that need* and dogfooded on it.

## Prior art, licenses & why build (not extend)
- **Can't build on the GDScript POC.** `hanse7962/GodotMutationTesting` is **unlicensed** (all rights
  reserved) — legally untouchable, and a dormant undocumented 0-star experiment anyway. Study for ideas only.
- **No pluggable engine to "just add GDScript to."** Stryker is a *family of separate per-language tools*
  (StrykerJS/.NET/Scala), not one core with a language-plugin API — adding GDScript ≈ writing a whole new
  Stryker. The one deliberately language-extensible tool, `universalmutator`, is **regex-based** (text-rule
  mutation → mostly-invalid mutants; worse than AST) and academic. So there's no good host to extend —
  a thin engine + a gdtoolkit AST adapter is genuinely the best path, not reinvention-for-its-own-sake.
- **The mature tools are permissive → learn freely.** Stryker/PIT/Mull **Apache-2.0**; mutmut/infection
  **BSD-3**; cosmic-ray/cargo-mutants **MIT**. Patterns aren't copyrightable and these even allow adapting
  code with attribution — so steal the architecture (coverage-guided selection, schemata, incremental, the
  report schema) openly.


- **This tool → MIT** (matches the norm; maximally adoptable).

## Patterns to steal (cross-language prior art)
- **Split mutator (AST) / runner (tests) / reporter** — the generic-engine + adapters design; every good
  tool does this (mutmut, Stryker, mutant).
- **Coverage-guided mutant selection** — only run tests that cover the mutated line. The #1 speedup (PIT, Stryker).
- **Mutant schemata / "switching"** — bake all mutants into one instrumented copy, toggle via a switch,
  avoid re-parsing/re-running per mutant (PIT).
- **Incremental / diff-scoped** — mutate only changed lines (the per-PR mode).
- **Adopt Stryker's [mutation-testing-elements](https://github.com/stryker-mutator/mutation-testing-elements)
  report schema** — then output renders in the existing HTML viewer for free.
- Study: [awesome-mutation-testing](https://github.com/theofidry/awesome-mutation-testing),
  [mutation-testing-in-patterns](https://github.com/atodorov/mutation-testing-in-patterns). Closest engine to
  copy the shape of: **mutmut** (Python + AST, like ours).

## Design goals
- **Ship fast.** A working v0.1 that mutates one real module and prints survivors beats a perfect framework.
- **Standalone. Usable by anyone — no Claude, no AI required.** A normal CLI a developer installs and runs,
  exactly like Stryker is in its domain. AI is *optional upside* (see modes), never a dependency. This is the
  #1 design constraint: **a non-AI developer must be able to pick it up and use it from the README alone.**
- **Generic engine, per-language adapters.** The loop (mutate → run tests → killed/survived → report) is
  identical in every language; only two bits are language-specific — mutating the AST, and running that
  language's tests. Build the loop once; a new language = one small adapter.
  - **GDScript adapter first** (the gap; via gdtoolkit's parser).
  - **TypeScript:** don't compete with Stryker — *delegate* to it, or skip. Adapters are independent.

## Architecture (the shape, not built yet)
```
engine/            language-neutral loop: select → mutate → run → tally → report; coverage-gated selection
  operators/       language-neutral operator CATALOG (boolean/comparison/const/arith swaps, stmt deletion)
adapters/
  gdscript/        gdtoolkit AST: apply operators → unparse; run `godot --headless` + GUT/GdUnit
  <lang>/          (future) one small module per language
cli/               the standalone entry point a non-AI dev runs
```
Two modes, one engine: a **deterministic operator core** (reproducible — the mode a merge-gate can trust) and,
later, an optional **LLM-semantic mode** (plausible-bug mutants: off-by-one, dropped-last-element, swallowed
error) for *hardening*, kept out of the gate because it's nondeterministic.

## Status
**Bootstrapped — hardening, toolchain, and the docs spine are in place; the engine is not built yet.**
Spun off from `project-rampart` (a Godot roguelike) so it has its own home and context. Next is the
`DESIGN.md` gate, then the engine loop + GDScript adapter — see NEXT STEPS and `ROADMAP.md`.

## Next steps
1. ✅ **Repo hardened + stack chosen.** Security baseline + Python CI (ruff / mypy / pytest+coverage /
   pip-audit, plus a gitleaks secret-scan). The engine is **Python + uv + gdtoolkit** (see
   `docs/decisions/0001`), with **GdUnit4** as the first test-runner adapter.
2. ✅ **Name cleared** — `gdmutant` is free on PyPI, npm, and GitHub (re-check + a trademark sense-check
   before any public launch).
3. **Write `DESIGN.md` (the design gate)** — goals, FG/NF requirements, the architecture (named metaphor +
   Mermaid + component-role table), build plan. Get it reviewed, *then* build the engine loop + adapter.
4. **Build v0.1 against a bundled `corpus/` fixture** — a small GDScript module + a GdUnit4 suite,
   reproducible and doubling as the tool's own regression tests. (`project-rampart` has no GDScript or
   tests yet, so fixture-first *is* the extract-from-use path; dogfood its real systems once they exist.)
5. Decide public timing (private now; flip when v0.1 mutates real code + shows survivors — never launch empty).

## Where it fits in your CI
This tool answers one question a green CI build can't: *"do the tests actually bite?"* It's an **advisory**
signal — report-mode, never a hard gate — complementary to coverage, run alongside whatever review and
CI a project already has. It shares no code with any reviewer tool; it's a standalone CLI on purpose.

## License
[MIT](LICENSE) — © 2026 Karsten Huttelmaier. Third-party licenses are logged in [CREDITS.md](CREDITS.md).
