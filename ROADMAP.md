# Roadmap

<!-- Prioritized big rocks — the backlog. Mark done items ~~struck~~ ✅. -->

## Now
- ~~Bootstrap: repo hardening + Python CI + package skeleton + docs spine~~ ✅
- **`DESIGN.md` — the design gate.** Goals, functional (FG-N) + non-functional (NF-N)
  requirements, the architecture (a named metaphor + a diagram + a component-role table),
  and the build plan. Reviewed *before* any engine code.

## Next (v0.1 — the minimal runnable milestone)
- **Engine loop:** select → mutate → run → tally → report (full suite per mutant).
- **Operator catalog:** boolean (`and`↔`or`), comparison (`>`↔`>=`), constant, arithmetic
  swaps, and statement deletion.
- **GDScript adapter** (opens with an unparse-fidelity spike): gdtoolkit AST → mutate →
  re-emit → run `godot --headless` + GdUnit4 → parse JUnit XML → killed/survived.
- **Bundled `corpus/` fixture:** a small GDScript module + a GdUnit4 suite. Doubles as the
  tool's own regression tests; proves v0.1 mutates real code and prints survivors.
- **Report** in the `mutation-testing-elements` JSON schema (renders in the HTML viewer).

## Later (deferred — do not build now)
- Coverage-gated mutant selection (the #1 speedup; GDScript coverage tooling is immature).
- HTML report output; incremental / diff-scoped (per-PR) mode.
- Optional LLM-semantic mutants (plausible-bug mode) — kept *out* of the deterministic path.
- A second-language adapter (TypeScript delegates to Stryker, or is skipped).
- Publish to PyPI (`pipx install gdmutant`); make the repo public once v0.1 shows survivors.
