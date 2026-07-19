# Method-body mutation coverage: a manual cosmic-ray spot-check, not a second CI job

## Status
Accepted.

## Context
gdmutant dogfoods its own Python suite with **mutmut 3.6** (`[tool.mutmut]`, the advisory
"Mutation testing (mutmut, advisory)" CI job). mutmut 3.x mutates **module-level functions only** —
it generates no mutants inside class-method bodies (`docs/mutation-testing.md`). So ~24 methods
(`GdUnit4Runner.run`/`command`, `CommandRunner.run`, `Mutant.apply`, the `MutationRun` properties,
`Span.__post_init__`, `SuiteResult.failed`/`passed`, the `replacements` impls) are unit-tested but
not *mutation-measured*. This is upstream and unresolved: mutmut issue #387 has been open since
May 2025, and mutmut's own docs say "if you want to mutate code outside of functions, you can try
using mutmut 2." (LOD-80.)

The candidate fix is **cosmic-ray**, evaluated hands-on against a copy of `engine/runner.py`:
- **It closes the gap.** On the module mutmut covered with 64 mutants (all in the one module-level
  function), cosmic-ray produced 95 — including 71 *inside* the three class methods mutmut skipped,
  and it surfaced real actionable survivors there (`capture_output=True`→`False`,
  `returncode == 0`→`<= 0`).
- **It is far heavier.** ~30× slower per mutant (a fresh `pytest` subprocess per mutant vs mutmut's
  in-process caching); a multi-step SQLite session workflow (`init` → `exec` → `cr-report`) with its
  own TOML config; and it mutates things gdmutant deliberately treats as equivalent (type
  annotations under `from __future__ import annotations`, slice bounds), so adopting it means
  curating a second, larger equivalent-mutant catalog on top of the 18 already justified.

Wiring a second, 30×-slower mutation job into per-commit CI — to measure 24 methods that are already
unit-tested — is exactly the over-engineering gdmutant's "finishing beats features / minimal-but-real
tooling" values warn against. But pure document-and-defer leaves the method bodies permanently
*unmeasurable*, and the evaluation proved cosmic-ray both closes the gap and finds real survivors.

## Decision
Do **not** add cosmic-ray to CI. Keep the honest scope disclosure in `docs/mutation-testing.md`, and
add a **manual, on-demand cosmic-ray spot-check** for the method bodies: a committed scoped config
(`cosmic-ray.toml`) plus a recipe. cosmic-ray stays a throwaway `uv run --with cosmic-ray`
invocation (the project env, so gdmutant is importable), not a dev dependency, so the standing
toolchain and lockfile stay lean. Run it by hand when the method-heavy
modules change (commit first — like mutmut and gdmutant itself, cosmic-ray mutates in place).

## Consequences
- The method bodies become **spot-checkable on demand** without any per-commit CI cost or a second
  flaky advisory job — the minimal-but-real middle path.
- mutmut remains the CI dogfood; its score is still read as "every behavioral mutant mutmut generates
  is killed," over the module-level surface, exactly as documented.
- The spot-check is opt-in effort: if the method bodies regress, only a manual run catches it. That
  is an accepted trade — the methods are unit-tested, and the recipe makes a deeper check cheap when
  it matters.
- Re-evaluate if a second language adapter or a heavier method-level regression makes standing
  method-body measurement worth the CI budget.
