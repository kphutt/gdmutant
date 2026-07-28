---
type: decision
status: active
created: 2026-07-18
---

# Operator-scoped `# gdmutant: ignore` + an `Ignored` report status

## Status
Accepted — refines [0004](0004-equivalent-mutant-ignore-annotation.md).

## Context
[ADR-0004](0004-equivalent-mutant-ignore-annotation.md) chose an inline `# gdmutant: ignore`
annotation, scoped to the **physical line**, that made the adapter generate **no mutants** for that
line (they vanished from the report). Two problems surfaced the first time gdmutant ran against a
real project (project-rampart):

1. **Line scope is too coarse.** A single line often holds mutants from *different* operators — e.g.
   `if value < 0:` yields a `comparison` mutant (`<`→`<=`) *and* two `numeric` mutants (`0`→`1`,
   `0`→`-1`). On rampart's modules, **every** equivalent survivor shared its line with a *killed* or
   *timeout* mutant, so line-scoped suppression would have hidden genuine coverage. The fixer loop
   could not cleanly terminate.
2. **Dropping is invisible.** A suppressed mutant simply disappeared. There was no way to see *what*
   was suppressed or *why*, and 0004 had explicitly rejected the report-status option (c).

## Decision
Refine the annotation and surface suppressed mutants instead of dropping them.

- **Operator scope.** `# gdmutant: ignore[<operator>]` suppresses only the named operator's
  mutant(s) on the line (`mutatorName` in the report); comma-list several — `ignore[comparison,
  numeric]`. A **bare** `# gdmutant: ignore` keeps its 0004 meaning: **every** operator on the line
  (backward-compatible). Scope is still the *physical line*, like `# noqa` / `# type: ignore[code]`.
- **Reason.** Trailing text after the marker/brackets is the human reason:
  `# gdmutant: ignore[comparison] equivalent at the boundary`.
- **Surface, don't drop** — reversing 0004's rejected option (c). A suppressed mutant is still
  *generated* but **never run**; it is classified `Ignored` (Stryker's `Ignored` `MutantStatus`),
  carries its reason as the schema's `statusReason`, and is **excluded from the mutation score**
  (numerator *and* denominator) — so suppressing a proven equivalent never dents the score. It also
  covers the "benign / brittle-to-kill" case (a reasoned `Ignored`, distinct from a proven
  equivalent) without a separate status.
- **Where it lives:** unchanged from 0004 — the adapter (`# …` is GDScript comment syntax); the
  engine stays language-neutral (a generic `Mutant.ignore_reason` the adapter sets).
- **Typo guard:** an `ignore[<name>]` naming an operator no catalog produces suppresses nothing; the
  CLI **warns** (it does not fail) so a silent no-op is visible.

## Consequences
- The fixer loop terminates on real multi-operator lines: suppress the equivalent, keep the killer.
- Reports now *show* suppressions (with reasons), so a reader sees what was excluded and why.
- 0004 stays as the origin record; this ADR supersedes its "no mutants generated" and line-only-scope
  points. Whole-statement scope remains future work.
