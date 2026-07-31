---
type: reference
status: active
---

# Survivor explainers

A **surviving mutant** is a change gdmutant made to your source that every test still passed —
proof that the behavior on that line isn't actually checked (coverage says the line *ran*; mutation
says the result isn't *asserted*). A survivor isn't a bug in your code, and it isn't a bug in
gdmutant — it's a **gap in your tests**, a specific, located "here's a change nothing caught."

Each section below explains one mutation operator: what the change is, why a survivor matters, how
to kill it, and when it legitimately survives (an *equivalent mutant*). The `more` link in each
survivor points to that operator's section here.

**The score.** Mutation score = detected ÷ (detected + survived), where detected = killed + timeouts
(a mutation that hung the suite was caught, so a timeout counts as a kill). Three more categories
show in the summary but never enter that formula:

- **ignored** — suppressed by a `# gdmutant: ignore` annotation; generated for the report but never
  run against your tests.
- **invalid** — the mutant didn't parse; gdmutant's re-parse guard caught a broken mutation before it
  ever reached your tests.
- **error** — your test runner failed to execute this mutant (a crash, not a pass or a fail); tallied
  on its own so one bad run doesn't discard the whole pass.

There's no universal "good" score — it depends on how gnarly the code under test is. Watch the
direction it moves, not the absolute number: a rising score as you kill survivors is progress, and a
low score on code you just wrote is more urgent than a stable score on code nobody's touched in
months.

The rule of thumb for every operator: **your tests pass whether the code is the original or the
mutant — so if the mutated behavior would be wrong, nothing guards against it.** Only you know the
intended result; gdmutant reports the gap, not the answer.

Jump to an operator: [Arithmetic](#arithmetic) · [Boolean](#boolean) · [Comparison](#comparison) ·
[Compound assign](#compound-assign) · [Constant](#constant) · [Logical not](#logical-not) ·
[Modulo](#modulo) · [Numeric](#numeric) · [Statement deletion](#statement-deletion)

## Arithmetic

**The change:** gdmutant swapped an arithmetic operator (e.g. `+` → `-`, `*` → `/`). Note `+` may also concatenate strings.

**Why it survived:** nothing pins the exact result, so the two operators produce values your tests treat the same (they check the sign, or "non-zero", but not the number).

**How to kill it:** add a test with concrete inputs and assert the **exact** expected result.

**Equivalent mutant?** Possible when the operands make both operators yield the same value (e.g. `x * 1` vs `x / 1`, or `x + 0`).

## Boolean

**The change:** gdmutant swapped `and` ↔ `or`.

**Why it survived:** `and` and `or` return the same result **except** when the two operands disagree (one true, one false). No test exercised that case, so the connective is unchecked.

**How to kill it:** add a test where exactly one side is true and the other false, and assert the outcome — that is the only input that distinguishes `and` from `or`.

**Equivalent mutant?** If one operand can never be false (or never true) at this point, `and` and `or` are equivalent and the survivor is legitimate.

## Comparison

**The change:** gdmutant swapped a comparison operator (e.g. `>` → `>=`, `<` → `<=`, `==` → `!=`).

**Why it survived:** `>` and `>=` (and their kin) differ on exactly one input — when the two sides are **equal**. Your tests run this line but never with equal operands, so the boundary is untested.

**How to kill it:** add a test that reaches this line with two equal operands (a value compared to itself) and assert the result you intend. That case fails under the mutant.

**Equivalent mutant?** Rare here, but possible if the equal case is genuinely unreachable (e.g. the two operands can never be equal by construction). If so, the survivor is legitimate.

## Compound assign

**The change:** gdmutant swapped a compound-assignment operator (e.g. `+=` → `-=`).

**Why it survived:** nothing pins the accumulated value, so the two updates look the same to your tests.

**How to kill it:** add a test that drives several updates through this line and asserts the exact accumulated value.

**Equivalent mutant?** Possible when the accumulator is never observed, or the update amount is zero.

## Constant

**The change:** gdmutant flipped a boolean literal (`true` ↔ `false`).

**Why it survived:** nothing your tests assert depends on this value, so its actual value is invisible to the suite.

**How to kill it:** add a test that exercises the behavior this flag/value controls and assert it matches the value.

**Equivalent mutant?** If the value never affects observable behavior (dead flag), the survivor is legitimate — consider removing the constant.

## Logical not

**The change:** gdmutant removed a `not`, inverting a condition.

**Why it survived:** no test runs this branch with the condition both ways, so the inversion changes nothing your tests observe.

**How to kill it:** add a test that makes the condition true and another that makes it false, and assert which branch runs each time.

**Equivalent mutant?** If the guarded branch has no observable effect, the survivor is legitimate.

## Modulo

**The change:** gdmutant swapped `%` with another operator (e.g. `*`, `/`).

**Why it survived:** every test input is a clean multiple, where `%`, `*`, and `/` can produce indistinguishable results.

**How to kill it:** add a test with a **non-multiple** input (one that leaves a remainder) and assert the exact result.

**Equivalent mutant?** Rare; possible if the operand is always a multiple by construction.

## Numeric

**The change:** gdmutant changed a numeric literal (e.g. `0` → `1`, bumped a bound).

**Why it survived:** no test pins the exact value or the boundary this number sets.

**How to kill it:** add tests on each side of the boundary this number controls, and assert which side each input lands on.

**Equivalent mutant?** If the literal only affects an internal value that never changes an observable outcome, the survivor may be legitimate.

## Statement deletion

**The change:** gdmutant removed a whole statement (replaced it with `pass`).

**Why it survived:** nothing your tests assert depends on this statement running, so its entire effect is unchecked.

**How to kill it:** add a test that asserts the effect of this line — a signal emitted, a field set, a call made — something that fails if the line is gone.

**Equivalent mutant?** Legitimate if the statement genuinely has no observable effect (dead code) — consider removing it.
