# gdmutant

> **`gdmutant` is a provisional codename** — clear it (no existing tool uses it) before any public
> launch, exactly like a game title. It lives in ONE place: this README + the repo name (`gh repo rename`
> while private is free).

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
- **A money path is proven in this exact domain.** `mbj/mutant` (2.1k★) ships a **commercial license** —
  evidence a mutation tool *can* monetize later via open-core, if ever wanted. Not a decision now.
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

## North star (the product bar)
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
**Seed only — no code yet.** This repo was spun off from `project-rampart`'s planning so it has its own home
and context. Design rationale is captured across `project-rampart` ADR-0002 + `docs/agent-workflow/confidence-signals.md`
(Litmus/mutation split) — this is the standalone continuation.

## NEXT STEPS (for a fresh session picking this up cold)
1. **Harden the repo** — run the house baseline (`~/dev/ai-toolkit/docs/repo-hardening-checklist.md`): §1
   spine + pick the engine's stack (Python is natural — gdtoolkit is Python — so the engine + GDScript
   adapter can share a runtime; wire pip-audit/ruff/pytest CI). Add the standard docs skeleton.
2. **Clear the name** (no existing tool uses `gdmutant`; check PyPI/npm/GitHub) or rename.
3. **Build the engine loop** + the **GDScript adapter** against a real `project-rampart` module (extract-from-use).
4. **Dogfood on rampart's procgen-connectivity + turn-scheduler tests** — the original need.
5. Decide public timing (private now; flip when v0.1 mutates real code + shows survivors — never launch empty).

## Relationship to Litmus (a peer, not a parent)
Litmus (the grounded PR reviewer, in `ai-toolkit/prompts/litmus/`) and this tool are **two advisory signals
in the same merge gate** — *"did we build the right thing?"* (Litmus) vs *"do the tests actually bite?"*
(gdmutant) — but they share **no code**. This is its own repo on purpose. Other repos merely *reference* it.
