# gdmutant

> **`gdmutant` is a provisional codename** — clear it (no existing tool uses it) before any public
> launch, exactly like a game title. It lives in ONE place: this README + the repo name (`gh repo rename`
> while private is free).

**A mutation-testing tool — the first for GDScript/Godot, built to be language-agnostic.** Point it at a
codebase with a test suite; it mutates the source (flip `>`↔`>=`, `and`↔`or`, drop a `return`, …), reruns
the tests per mutant, and reports **survivors** — lines a bug could live on and no test would catch. Coverage
says a line *ran*; mutation says a bug there would be *caught*. That gap is the product.

## Why this exists (the opening)
- **No mutation tester exists for GDScript.** [Stryker](https://stryker-mutator.io/) does JS/TS, C#, Scala;
  mutmut does Python; PIT does Java. GDScript has **nothing** — and the AST work is nearly free now that
  [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit) ships a real GDScript parser.
- **AI just opened the demand.** When an AI writes the tests, coverage is *especially* a lie (models write
  tests that pin the code they just wrote). Mutation is one of the few **executable, model-independent**
  signals that a test actually bites. So under-tested ecosystems like game-dev suddenly need this.
- **Extracted from real use, not built speculatively.** The driver is `project-rampart` (a Godot roguelike)
  needing to trust its two hardest systems (turn scheduler + procgen connectivity). This tool gets *extracted
  from that need* and dogfooded on it.

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
