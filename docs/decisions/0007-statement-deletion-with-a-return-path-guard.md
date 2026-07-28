---
type: decision
status: active
created: 2026-07-18
---

# Statement-deletion via a generation-time return-path guard

## Status
Accepted.

## Context
FG-2.1 requires a **statement-deletion** operator (replace a statement with `pass`). Unlike every
existing operator — a token→token swap that stays valid GDScript — deleting a statement can produce
source that **parses but does not compile**. Concretely: deleting the `return` from a *typed*
function (`func f() -> bool: return x` → `func f() -> bool: pass`) is a Godot **compile error**
("not all code paths return a value").

gdmutant's NF-5 validity guard re-parses each mutant with **gdtoolkit**, which has *no return-path
analysis* — so it accepts these mutants, and Godot then rejects them at load. Observed running
against the corpus: such a mutant makes the CommandRunner harness hang (→ `Timeout`, counted as
killed) and the GdUnit4 runner write no report (→ `error`, excluded) — the **same mutant, a
different verdict per runner**, breaking the self-test's "both paths agree" invariant, and burning a
full timeout each. This is the DESIGN.md NF-5 "oracle boundary" (gdtoolkit accepts / Godot rejects),
which statement-deletion hits systematically. External mutation-testing practice is unanimous: an
uncompilable mutant "adds no value" and should not be generated.

Options considered: (1) emit everything and accept the timeout/error noise; (2) delete only
expression statements, never returns — but the corpus is return-heavy (zero `expr_stmt` sites), so
the operator would go untested there, and it discards return-deletion, a classic strong mutation;
(3) validate each mutant with `godot --check-only` — but that exits 0 even on the parse error
(verified), needs brittle stderr-scraping, and puts Godot in the "mutation half (no Godot)" module.

## Decision
Emit deletions for expression statements **and** returns, but **guard return-deletion at generation
time** in the adapter — validity for this operator class lives at generation, not the re-parse gate,
because gdtoolkit can't police it. A `return` is emitted only when deleting it cannot break
compilation:

- the enclosing function is **untyped or `-> void`** (no return-value requirement), **or**
- the function body's **last top-level statement is a *different* `return`** — a guaranteed final
  return backstops the deletion, so every path still returns a value.

Otherwise (a typed function's sole/final return, or one whose last top-level statement isn't a
return) the deletion is **skipped** — conservative, sound by construction. Function scopes are
analysed independently, so a `return` inside a lambda is judged by the lambda's own rules, not the
enclosing function's — and because a `lambda_header` carries the same optional `-> TYPE_HINT` as a
`func_header`, a **typed lambda** (`func() -> int: return 9`) is guarded exactly like a typed
function (deleting its return is the same Godot "not all code paths return a value" error, verified
via `--check-only`). Declarations (`func_var_stmt`) are deferred (deleting one breaks a later
reference or is an equivalent — both noise). Multi-line statements are skipped (`spans.py` edits a
single line). This mirrors the adapter's existing `_string_format_percents` precedent: tree-aware
suppression of a known-broken mutant class, in the conservative direction.

## Consequences
- Every emitted deletion loads in Godot, so both runner paths agree and no mutant burns a timeout —
  the self-test invariants hold, extended with a `--check-only` oracle that boots every emitted
  deletion through Godot as a standing soundness check (the falsifiable guard for this rule).
- The guard embeds a *partial* model of Godot's flow analysis. The failure mode to watch is
  **unsoundness** — an emitted deletion Godot still rejects. Typed lambdas are now covered (a
  dedicated adversarial `--check-only` self-test exercises the path the corpus lacks); the remaining
  candidates are `match` exhaustiveness and a future Godot tightening. The `--check-only` oracle in
  the self-test is exactly the check that would catch it; if it ever fails, tighten the rule or
  narrow to expression-only.
- In fully-typed code, a function with a sole `return` gets no deletion mutant. Accepted for v0.1;
  richer flow analysis (or an opt-in "aggressive" mode) is future work.

Decision reached via a structured design-review method on the design fork (grounded against the real corpus,
runners, and Godot). Independent follow-up: harden the corpus harness to `quit()` on a load error so
a *future* broken script fails fast rather than hanging (a CommandRunner-adoption gotcha).
