---
type: decision
status: superseded
created: 2026-07-19
---

# Method-body mutation coverage: a manual cosmic-ray spot-check, not a second CI job

## Status
Accepted, then superseded 2026-07-23: the manual cosmic-ray spot-check and its committed
`cosmic-ray.toml` were removed as unused clutter, leaving mutmut as the sole mutation check. The
analysis below is kept as history. Corrected 2026-07-31: the blind spot this record measured is
still exactly as wide as recorded, but the reason given for it was never the real one. See
[Correction](#correction-2026-07-31).

## Context
gdmutant dogfoods its own Python suite with mutmut 3.6 (`[tool.mutmut]`, the advisory
"Mutation testing (mutmut, advisory)" CI job). mutmut 3.x mutates module-level functions only:
it generates no mutants inside class-method bodies (`docs/mutation-testing.md`). So ~24 methods
(`GdUnit4Runner.run`/`command`, `CommandRunner.run`, `Mutant.apply`, the `MutationRun` properties,
`Span.__post_init__`, `SuiteResult.failed`/`passed`, the `replacements` impls) are unit-tested but
not *mutation-measured*. This is a known mutmut 3.x limitation, its own docs say "if you want to
mutate code outside of functions, you can try using mutmut 2", and it is reproduced below (0 mutants
in any class method).

The candidate fix is cosmic-ray, evaluated hands-on:
- It closes the gap. On `engine/runner.py` (the evaluation vehicle, a copy with a trivial
  suite), mutmut generated 64 mutants (all in the one module-level function) while cosmic-ray produced
  95, including 71 *inside* the three class methods mutmut skipped (`CommandRunner.run`=55,
  `SuiteResult.failed`=15, `.passed`=1), surfacing real survivors there (`capture_output=True`→`False`,
  `returncode == 0`→`<= 0`). And on the shipped config's target `engine/mutants.py`, cosmic-ray finds
  20 mutation points, 16 of them inside `Mutant.apply`/`describe_change` (mutmut: 0), 5 surviving
  (25%) against the real suite, the same gap on the module this ADR ships a config for.
- It is far heavier. Roughly 30x slower per mutant (a fresh `pytest` subprocess per mutant against
  mutmut's in-process caching), a multi-step SQLite session workflow (`init` → `exec` → `cr-report`)
  with its own TOML config, and it mutates things gdmutant deliberately treats as equivalent (type
  annotations under `from __future__ import annotations`, slice bounds), so adopting it means
  curating a second, larger equivalent-mutant catalog on top of the 18 already justified.

Wiring a second, 30x-slower mutation job into per-commit CI, to measure 24 methods that are already
unit-tested, is exactly the over-engineering gdmutant's "finishing beats features / minimal-but-real
tooling" values warn against. But pure document-and-defer leaves the method bodies permanently
*unmeasurable*, and the evaluation proved cosmic-ray both closes the gap and finds real survivors.

## Decision
Do not add cosmic-ray to CI. Keep the honest scope disclosure in `docs/mutation-testing.md`, and
add a manual, on-demand cosmic-ray spot-check for the method bodies: a committed scoped config
(`cosmic-ray.toml`) plus a recipe. cosmic-ray stays a throwaway `uv run --with cosmic-ray`
invocation (the project env, so gdmutant is importable), not a dev dependency, so the standing
toolchain and lockfile stay lean. Run it by hand when the method-heavy
modules change (commit first, because like mutmut and gdmutant itself, cosmic-ray mutates in place).

## Consequences
- The method bodies become spot-checkable on demand without any per-commit CI cost or a second
  flaky advisory job, the minimal-but-real middle path.
- mutmut remains the CI dogfood, and its score is still read as "every behavioral mutant mutmut
  generates is killed," over the module-level surface, exactly as documented.
- The spot-check is opt-in effort. If the method bodies regress, only a manual run catches it. That
  is an accepted trade: the methods are unit-tested, and the recipe makes a deeper check cheap when
  it matters.
- Re-evaluate if a second language adapter or a heavier method-level regression makes standing
  method-body measurement worth the CI budget.

## Correction (2026-07-31)

Two places in this record explain *why* mutmut reaches none of gdmutant's class-method bodies. The
Context asserts that mutmut

> mutates module-level functions only

and the Status note above originally closed by saying that

> the mutmut-3.x method-body limitation it documents still holds

The observation was right, and still is. The explanation was not, and was never right.

Read against mutmut 3.6.0 as installed (`mutmut/mutation/file_mutation.py`), the mutation pass walks
into a `ClassDef` and builds trampolines for the methods it finds, so an ordinary class method is
mutated like any other function. Checked directly: a two-method class carrying no decorators yields
one mutant per method. What mutmut actually refuses is a *decorated* class or a *decorated*
function, with `@staticmethod` and `@classmethod` the only exemptions, because copying a decorated
definition for the trampoline can re-run the decorator.

That distinction matters here because gdmutant is built almost entirely out of frozen dataclasses.
Of its 25 classes, 22 carry a decorator, and the 3 that do not (`BaselineFailed`, `Verdict`,
`SuiteTimeout`) declare no methods at all. So the blind spot this ADR set out to close is real and
is still exactly as wide as recorded, for a reason this record misnames.

Measured on 2026-07-31 against the tree of the day: mutmut 3.6.0 generates 3,773 mutants across
`gdmutant/`, and 0 of them fall inside a class-method body. Replaying the same measurement against
the tree as it stood when this repo first recorded a mutant total gives 781, again with 0 inside a
class-method body. The count grew with the package (1,197 lines of `gdmutant/` then, 6,143 now)
rather than with mutmut's reach, which has been pinned at 3.6.0 throughout.

The decision this ADR reached is untouched. It declined to put a second, far slower mutation job in
CI and reached for an on-demand check instead, and that argument rests on the gap being real and on
the cost of standing measurement, both of which hold at today's measurement. What changes is only
the sentence a reader would otherwise carry away about mutmut: it is not module-level-only, it is
decorator-shy.
