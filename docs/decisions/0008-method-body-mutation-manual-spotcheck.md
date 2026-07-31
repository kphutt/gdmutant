---
type: decision
status: superseded
created: 2026-07-19
---

# Method-body mutation coverage: a manual cosmic-ray spot-check, not a second CI job

## Status
Accepted — **superseded 2026-07-23**: the manual cosmic-ray spot-check and its committed
`cosmic-ray.toml` were removed as unused clutter; mutmut remains the sole mutation check. The
analysis below is kept as history (the mutmut-3.x method-body limitation it documents still holds).

## Context
gdmutant dogfoods its own Python suite with **mutmut 3.6** (`[tool.mutmut]`, the advisory
"Mutation testing (mutmut, advisory)" CI job). mutmut 3.x mutates **module-level functions only** —
it generates no mutants inside class-method bodies (`docs/mutation-testing.md`). So ~24 methods
(`GdUnit4Runner.run`/`command`, `CommandRunner.run`, `Mutant.apply`, the `MutationRun` properties,
`Span.__post_init__`, `SuiteResult.failed`/`passed`, the `replacements` impls) are unit-tested but
not *mutation-measured*. This is a known mutmut 3.x limitation — its own docs say "if you want to
mutate code outside of functions, you can try using mutmut 2" — and is reproduced below (0 mutants
in any class method).

The candidate fix is **cosmic-ray**, evaluated hands-on:
- **It closes the gap.** On `engine/runner.py` (the evaluation vehicle — a copy, with a trivial
  suite), mutmut generated 64 mutants (all in the one module-level function) while cosmic-ray produced
  95, including 71 *inside* the three class methods mutmut skipped (`CommandRunner.run`=55,
  `SuiteResult.failed`=15, `.passed`=1), surfacing real survivors there (`capture_output=True`→`False`,
  `returncode == 0`→`<= 0`). And on the shipped config's target `engine/mutants.py`, cosmic-ray finds
  20 mutation points, **16 inside `Mutant.apply`/`describe_change`** (mutmut: 0), 5 surviving (25%)
  against the real suite — the same gap, on the module this ADR ships a config for.
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
