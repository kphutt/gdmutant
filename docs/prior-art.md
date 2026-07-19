# Prior art & why build

Context for why gdmutant exists as a new tool rather than an extension of an existing one, and the
cross-language patterns it borrows. (Relocated from the README to keep that page product-focused.)

## The GDScript gap

No *usable* mutation tester exists for GDScript. [Stryker](https://stryker-mutator.io/) covers
JS/TS, C#, and Scala; [mutmut](https://github.com/boxed/mutmut) covers Python; PIT covers Java.
GDScript has only a dormant, unlicensed proof-of-concept
([hanse7962/GodotMutationTesting](https://github.com/hanse7962/GodotMutationTesting) — a few weeks
of work in Apr–May 2026, no README, no license), so the space for a documented, adopted tool is
open. The AST work is nearly free now that
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit) ships a real GDScript parser.

## Why build, not extend

- **Can't build on the GDScript POC.** `hanse7962/GodotMutationTesting` is unlicensed (all rights
  reserved) — legally untouchable, and a dormant undocumented experiment anyway. Study for ideas
  only.
- **There is no pluggable engine to "just add GDScript to."** Stryker is a family of separate
  per-language tools (StrykerJS / .NET / Scala), not one core with a language-plugin API — adding
  GDScript would be writing a whole new Stryker. The one deliberately language-extensible tool,
  `universalmutator`, is regex-based (text-rule mutation → mostly-invalid mutants; worse than AST)
  and academic. So a thin engine plus a gdtoolkit AST adapter is genuinely the best path, not
  reinvention for its own sake.
- **The mature tools are permissively licensed → learn freely.** Stryker / PIT / Mull are
  Apache-2.0; mutmut / Infection are BSD-3; cosmic-ray / cargo-mutants are MIT. Patterns aren't
  copyrightable and these licenses even allow adapting code with attribution, so the architecture
  (coverage-guided selection, schemata, incremental mode, the report schema) can be borrowed openly.

gdmutant itself is [MIT](../LICENSE) — matching the norm and maximally adoptable.

## Patterns borrowed from mature tools

- **Split mutator (AST) / runner (tests) / reporter** — the generic-engine + adapters design every
  good tool uses (mutmut, Stryker, mutant).
- **Coverage-guided mutant selection** — run only the tests that cover the mutated line; the #1
  speedup (PIT, Stryker).
- **Mutant schemata / "switching"** — bake all mutants into one instrumented copy and toggle via a
  switch, avoiding a re-parse/re-run per mutant (PIT).
- **Incremental / diff-scoped** — mutate only changed lines (the per-PR mode).
- **Stryker's [mutation-testing-elements](https://github.com/stryker-mutator/mutation-testing-elements)
  report schema** — so output renders in the existing HTML viewer for free.

Further reading:
[awesome-mutation-testing](https://github.com/theofidry/awesome-mutation-testing),
[mutation-testing-in-patterns](https://github.com/atodorov/mutation-testing-in-patterns). The
closest engine in shape is **mutmut** (Python + AST, like gdmutant).
