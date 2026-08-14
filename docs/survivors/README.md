---
type: reference
status: active
created: 2026-07-19
---

# Survivor explainers

A surviving mutant is a change gdmutant made to your source that every test still passed,
proof that the behavior on that line isn't actually checked (coverage says the line *ran*, mutation
says the result isn't *asserted*). A survivor isn't a bug in your code, and it isn't a bug in
gdmutant. It's a gap in your tests, specific and located: "here's a change nothing caught."

Each section below explains one mutation operator: what the change is, why a survivor matters, how
to kill it, and when it legitimately survives (an *equivalent mutant*). The `more` link in each
survivor points to that operator's section here. Two sections, [Assert](#assert) and
[Enum member](#enum-member), are not operators: each explains a whole class of survivor that is
unkillable by where it sits rather than by which operator produced it, and gdmutant links a survivor
there instead when that is the real story. Every one of those mutants still runs and still counts
toward the score. Only its explanation differs.

### The score

Mutation score = detected ÷ (detected + survived), where detected = killed + timeouts
(a mutation that hung the suite was caught, so a timeout counts as a kill). Three more categories
show in the summary but never enter that formula:

- ignored: suppressed by a `# gdmutant: ignore` annotation, generated for the report but never
  run against your tests.
- invalid: the mutant didn't parse, and gdmutant's re-parse guard caught a broken mutation before it
  ever reached your tests.
- error: your test runner failed to execute this mutant (a crash, not a pass or a fail), tallied
  on its own so one bad run doesn't discard the whole pass.

### What is never generated, and why that moves the score

A few token positions produce a mutant
that is not a *changed program* at all. In the first three shapes below the mutant is code GDScript
rejects, so no test could ever have disagreed with it. In the fourth it is valid GDScript that is
*inert*: it stores a value nothing can read back, so it behaves exactly like the original whatever
your tests do. In the fifth it is valid GDScript that was never arithmetic in the first place: the
`/` in `$Sprite2D/Label` separates the steps of a path to a node, so changing it names a different
node rather than computing a different number. gdmutant does not generate any of them, which means
they appear in no
category above: they leave the denominator entirely, and the score is higher than it would be if
they were counted as survivors. That is the honest number, not a flattering one. A mutant the
language has already settled measures nothing about your tests, and counting it as a gap would say
your suite misses something it cannot possibly catch. The excluded shapes:

- a `%` that is string formatting (`"%s" % name`), not modulo
- a `+` that concatenates strings, because GDScript's `String` defines no `-`
- a `+=` that appends to a string, for the same reason
- a property declaration's initializer whose stored value no getter can read back (the inert shape)
- a `/` or `%` that punctuates a node path (`$Sprite2D/Label`, `%HealthBar`), which is not an operator

Each is decided from the parse tree, and only where the shape is certain: a `String`-typed
*variable* is still mutated, because nothing in the source proves what it holds. The bias is
deliberate: reporting noise is a smaller failure than hiding a real gap, so anything ambiguous stays
in. Everything else your code does is mutated and counted.

There's no universal "good" score. It depends on how gnarly the code under test is. Watch the
direction it moves, not the absolute number: a rising score as you kill survivors is progress, and a
low score on code you just wrote is more urgent than a stable score on code nobody's touched in
months.

The rule of thumb for every operator: your tests pass whether the code is the original or the
mutant, so if the mutated behavior would be wrong, nothing guards against it. Only you know the
intended result. gdmutant reports the gap, not the answer.

Jump to an operator: [Arithmetic](#arithmetic) · [Boolean](#boolean) · [Comparison](#comparison) ·
[Compound assign](#compound-assign) · [Constant](#constant) · [Logical not](#logical-not) ·
[Modulo](#modulo) · [Numeric](#numeric) · [Statement deletion](#statement-deletion)

Not an operator: [Assert](#assert) · [Enum member](#enum-member)

## Arithmetic

**The change:** gdmutant swapped an arithmetic operator (e.g. `+` → `-`, `*` → `/`). Note `+` may also concatenate strings.

**Why it survived:** nothing pins the exact result, so the two operators produce values your tests treat the same (they check the sign, or "non-zero", but not the number).

**How to kill it:** add a test with concrete inputs and assert the exact expected result.

**Equivalent mutant?** Possible when the operands make both operators yield the same value (e.g. `x * 1` vs `x / 1`, or `x + 0`).

## Boolean

**The change:** gdmutant swapped `and` ↔ `or`.

**Why it survived:** `and` and `or` return the same result except when the two operands disagree (one true, one false). No test exercised that case, so the connective is unchecked.

**How to kill it:** add a test where exactly one side is true and the other false, and assert the outcome. That is the only input that distinguishes `and` from `or`.

**Equivalent mutant?** If one operand can never be false (or never true) at this point, `and` and `or` are equivalent and the survivor is legitimate.

## Comparison

**The change:** gdmutant swapped a comparison operator (e.g. `>` → `>=`, `<` → `<=`, `==` → `!=`).

**Why it survived:** which pair was swapped decides what your tests are missing. `>` against `>=`, or `<` against `<=`, differ on exactly one input, when the two sides are equal, and your tests run this line but never with equal operands. `==` against `!=` is the other case: those two are opposites and disagree on every input, so a survivor there means nothing your tests assert reads this comparison at all.

**How to kill it:** add a test that reaches this line with two equal operands (a value compared to itself) and assert the result you intend. Equal operands separate every swap this operator makes, the boundary pair and the equality pair alike, so that one test kills any of them.

**Equivalent mutant?** Rare here. A boundary swap is one when the equal case is genuinely unreachable (e.g. the two operands can never be equal by construction). An `==` against `!=` swap disagrees on every input, so it is one only when nothing observable depends on the comparison at all.

## Compound assign

**The change:** gdmutant swapped a compound-assignment operator (e.g. `+=` → `-=`). A `+=` that appends a string literal gets no compound-assign mutant: `String` has no `-=`, so the swap would be invalid code rather than a test gap. That line can still get a statement-deletion mutant, which is a real gap when it survives.

**Why it survived:** nothing pins the accumulated value, so the two updates look the same to your tests.

**How to kill it:** add a test that drives several updates through this line and asserts the exact accumulated value.

**Equivalent mutant?** Possible when the accumulator is never observed, or the update amount is zero.

## Constant

**The change:** gdmutant flipped a boolean literal (`true` ↔ `false`).

**Why it survived:** nothing your tests assert depends on this value, so its actual value is invisible to the suite.

**How to kill it:** add a test that exercises the behavior this flag/value controls and assert it matches the value.

**Equivalent mutant?** If the value never affects observable behavior (dead flag), the survivor is legitimate. Consider removing the constant.

## Logical not

**The change:** gdmutant removed a `not`, inverting a condition.

**Why it survived:** no test runs this branch with the condition both ways, so the inversion changes nothing your tests observe.

**How to kill it:** add a test that makes the condition true and another that makes it false, and assert which branch runs each time.

**Equivalent mutant?** If the guarded branch has no observable effect, the survivor is legitimate.

## Modulo

**The change:** gdmutant swapped `%` with another operator (e.g. `*`, `/`).

**Why it survived:** nothing pins the exact result, so the two operators produce values your tests treat the same. A clean multiple is not the cause: `6 % 3` is 0 where `6 * 3` is 18 and `6 / 3` is 2, so once a test asserts the number, even a clean multiple tells the two apart.

**How to kill it:** add a test with concrete inputs and assert the exact result. An input that leaves a remainder makes the gap between the operators widest, but any input does once the result is pinned.

**Equivalent mutant?** Rare. It needs a left operand that is always 0, where `0 % n`, `0 * n` and `0 / n` all come out the same. An operand that is merely always a multiple does not make the mutant equivalent.

## Numeric

**The change:** gdmutant changed a number by one unit of its last written digit (e.g. `0` → `1`, `0.5` → `0.6`, `0xFF` → `0x100`, bumped a bound). Every literal form is covered: integers, floats, hex, binary, and separated forms like `1_000`. A float moves by one unit of the precision you wrote rather than by a whole `1.0`, so the mutant is the off-by-a-bit value the constant plausibly has, not one any test would reject on sight.

**Why it survived:** no test pins the exact value or the boundary this number sets.

**How to kill it:** add tests on each side of the boundary this number controls, and assert which side each input lands on.

**Equivalent mutant?** If the literal only affects an internal value that never changes an observable outcome, the survivor may be legitimate.

## Statement deletion

**The change:** gdmutant removed a whole statement (replaced it with `pass`).

**Why it survived:** nothing your tests assert depends on this statement running, so its entire effect is unchecked.

**How to kill it:** add a test that asserts the effect of this line (a signal emitted, a field set, a call made), something that fails if the line is gone.

**Equivalent mutant?** Legitimate if the statement genuinely has no observable effect (dead code). Consider removing it. The commonest shape by far is a redundant initializer: `_cells = PackedByteArray()` when the declaration `var _cells: PackedByteArray` already default-initialises it, or an assignment that just restates the declaration's own `=` value. Confirm one by checking that nothing can write to the variable before this line. The same statement inside a `reset()` that runs repeatedly is not redundant, and a test failing to catch its removal is a real gap.

## Assert

Not an operator. Any operator can land inside an `assert`, and when one does, the assert, not the
operator, is why the mutant survived.

**The change:** gdmutant changed a token inside an `assert(...)` call (a comparison, a connective, a number) or removed the assert statement outright.

**Why it survived:** a mutated assertion only behaves differently on an input the original would have rejected, and a failed `assert` aborts the whole Godot process. A test running inside that process cannot observe the abort as anything but its own death, so no test can pass on the original and fail on the mutant. The survivor is structural. It is not a gap in your suite.

**How to kill it:** from an in-process harness you cannot, and it is not worth trying. To take it out of the report, mark the line `# gdmutant: ignore`. It stays visible as `ignored` and leaves the score. gdmutant does not skip assert lines for you. A tool that quietly drops code from its own report is telling you a smaller truth than it knows, and which of your asserts are load-bearing is your call, not its.

**Equivalent mutant?** Effectively yes, and it is the common case rather than the exception: on defensive code, assert lines can hold most of a file's survivors and leave a healthy-looking score with nothing actionable behind it. If the condition is one real callers can actually violate, that is worth knowing, but the fix is to move the check into a branch that returns or emits an error, where a test can reach it, not to write a test that expects a crash.

## Enum member

Not an operator. A mutant lands inside an `enum` declaration, and the reason it survived is the
enum, not the operator. Most are `numeric` mutants on a member's value, but every mutant whose line
falls in that block is explained here, whichever operator made it.

**The change:** gdmutant changed a member of an `enum` declaration: usually the number assigned to it, and on a computed value like `A = 1 + 0` the arithmetic operator instead.

**Why it survived:** most enums are used symbolically. `if cell == Cell.FLOOR` compares a name to a name, and both sides move together when the number behind it changes, so nothing your tests observe reads the number at all.

**How to kill it:** only worth doing if the number is genuinely read as a number. Two cases where it is: a bitflag enum (`1, 2, 4`, combined with `|` and `&`), and a value that leaves the program: written to a save file, sent over a network, handed to a shader or another tool. There a changed number is a real bug, and a test that pins the concrete value (or round-trips it through whatever reads it) kills the mutant.

**Equivalent mutant?** Very often, and gdmutant cannot tell. It analyses one file at a time, so a numeric use in another file, in a save format, or in engine code is outside what it can see, and suppressing these by default would hide exactly the bitflag and serialisation bugs that matter most. So they are reported and the call is yours: `# gdmutant: ignore` on the line, with your reason, once you have checked.
