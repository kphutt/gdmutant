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

## Architecture (as built)
```
gdmutant/
  engine/          language-neutral loop: select → mutate → run → tally → mutation score
    operators/     operator catalog (boolean/comparison/arithmetic/constant/numeric-literal swaps)
    spans.py       AST-guided source-span editing (docs/decisions/0002)
    runner.py      the Runner interface + JUnit-XML parsing
    report.py      Stryker mutation-testing-elements JSON + a console summary
  adapters/
    gdscript/      gdtoolkit AST → locate token → mutate → NF-5 re-parse guard; the GdUnit4 runner
  cli.py           the standalone `gdmutant run` entry point (no AI required)
corpus/            a real GDScript fixture module + GdUnit4 suite (the end-to-end proof)
```
Two modes, one engine: a **deterministic operator core** (reproducible — the mode a merge-gate can trust) and,
later, an optional **LLM-semantic mode** (plausible-bug mutants: off-by-one, dropped-last-element, swallowed
error) for *hardening*, kept out of the gate because it's nondeterministic.

## Status
**v0.1 works — gdmutant mutates real GDScript and reports survivors end-to-end.** From a `.gd` file it
generates AST-based mutants (comparison / boolean / arithmetic / constant / numeric-literal), runs the
project's GdUnit4 suite per mutant, classifies killed / survived / invalid / error, computes a mutation
score, and emits a console summary + a Stryker `mutation-testing-elements` JSON report — via the
standalone `gdmutant run` CLI (no AI required). Proven end-to-end on the bundled `corpus/` module; the
**live `godot --headless` + GdUnit4** invocation is pending CI validation (see `ROADMAP.md`). Spun off
from `project-rampart` (a Godot roguelike) so it has its own home.

## Usage
```sh
uv sync --frozen   # install deps (or, once published: pipx install gdmutant)
uv run gdmutant run path/to/module.gd --project path/to/godot-project [--json report.json]
```
Prints each surviving mutant (`file:line` + the swap) and a mutation score, and optionally writes a
`mutation-testing-elements` report. Running the real suite needs Godot + the GdUnit4 addon on the project.

## Next steps
1. ✅ **Repo hardened + stack chosen.** Security baseline + Python CI (ruff / mypy / pytest+coverage /
   pip-audit, plus a gitleaks secret-scan). The engine is **Python + uv + gdtoolkit** (see
   `docs/decisions/0001`), with **GdUnit4** as the first test-runner adapter.
2. ✅ **Name cleared** — `gdmutant` is free on PyPI, npm, and GitHub (re-check + a trademark sense-check
   before any public launch).
3. ✅ **`DESIGN.md` design gate written + reviewed** — goals, FG/NF requirements, the "Saboteur & the
   Jury" architecture, and the build plan (`docs/design/DESIGN.md`).
4. ✅ **v0.1 built against the bundled `corpus/` fixture** — engine loop, operator catalog, GDScript
   adapter (NF-5 guard), GdUnit4 runner, Stryker reporter, and the `gdmutant run` CLI. Mutates
   `corpus/turn_order.gd` (13 mutants) and prints survivors end-to-end.
5. **Remaining before a public launch** (see `ROADMAP.md`): live CI Godot/GdUnit4 validation of the
   runner, the statement-deletion operator, then flip the repo public — never launch empty.

## Where it fits in your CI
This tool answers one question a green CI build can't: *"do the tests actually bite?"* It's an **advisory**
signal — report-mode, never a hard gate — complementary to coverage, run alongside whatever review and
CI a project already has. It shares no code with any reviewer tool; it's a standalone CLI on purpose.

## License
[MIT](LICENSE) — © 2026 Karsten Huttelmaier. Third-party licenses are logged in [CREDITS.md](CREDITS.md).
