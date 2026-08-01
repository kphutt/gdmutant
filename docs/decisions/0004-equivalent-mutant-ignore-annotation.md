---
type: decision
status: active
created: 2026-07-11
---

# Suppress equivalent mutants with an inline `# gdmutant: ignore` annotation

## Status
Accepted, refined by [0006](0006-operator-scoped-ignore-and-ignored-status.md), which adds
operator scope (`ignore[comparison]`) and surfaces suppressed mutants as `Ignored` (with a reason)
rather than dropping them.

## Context
Some mutants are equivalent: they change the source but not its observable behavior, so no test
can ever kill them (e.g. a clamp boundary where `<` and `<=` return the same value). They show up as
permanent survivors. That's noise for a human, and worse for an automated fixer. An agent told "kill
these survivors" burns cycles trying to kill mutants that are unkillable, and its loop never
converges. Mutation testers need a way to say "this one is expected to survive, stop reporting it."

Options for where a user records that:

- (a) Inline source annotation: a comment on the line, like `# noqa` or `# type: ignore`.
- (b) A config file listing mutants by `(file, line, operator)` or by id.
- (c) A verdict in the report only (mark as `Ignored`) with no way to opt a line out.

## Decision
Use (a), an inline `# gdmutant: ignore` comment. Any token on a source line whose text contains
that marker is skipped: the adapter generates no mutants for it, so it never appears as a
survivor and never affects the score.

### Granularity: the *physical* line
All operators on the annotated line are suppressed, matching how `# noqa` and `# type: ignore` work,
and the unit a human reasons about. A logical statement wrapped across multiple lines (a
parenthesized or `\`-continued condition) is not suppressed as a whole: mark each physical line
whose tokens you want excluded. This is a deliberate line-scoped design, and whole-statement scope
would need the enclosing AST node, so it is future work.

### Where it lives: the adapter, not the engine
`# …` is GDScript comment syntax, so the GDScript adapter (`find_sites`) detects it by scanning the
raw source (comments aren't tokens). The language-neutral engine stays unaware of it: no new
verdict, no engine change (respects NF-3).

### Excluded at generation, not run-then-`Ignored`
Simplest, and it's what makes the survivor set an agent sees *actionable*. Lines are split on `\n`
only, matching the engine's line counting (docs/decisions/0002), so the marker's line lines up with
token positions.

## Consequences
- An AI (or human) marks a genuinely-equivalent line with `# gdmutant: ignore` and the fixer loop
  can converge on the *real* survivors. This is the same distinction gdmutant already documents for
  its own dogfood (`docs/mutation-testing.md`).
- Trade-off, accepted: a raw-source substring check would also match the literal text inside a
  string on that line, a false-positive ignore. This is rare and easily spotted, and keeping the
  check simple (no comment tokenization) is worth it.
- Future option, not now: emit the suppressed mutants in the report with the schema's `Ignored`
  status instead of omitting them, for transparency in the HTML viewer. Deferred, because omission
  is enough for the convergence problem this solves.

## Alternatives rejected
- (b) config file. An id or line list is brittle, since ids and line numbers shift as code changes,
  so an agent maintaining it fights churn. The annotation lives *with* the line it excuses and moves
  with it.
