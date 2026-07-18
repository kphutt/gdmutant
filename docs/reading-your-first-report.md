---
type: guide
status: active
created: 2026-07-18
---

# Reading your first gdmutant report

You ran `gdmutant run …` and got a list of **survivors**. This is a short, human-facing guide to what
that means and what to do next. (Driving gdmutant from an AI agent instead? See
[`agent-guide.md`](agent-guide.md).)

## What a survivor is

gdmutant changes your source one tiny edit at a time — flip `>` to `>=`, `and` to `or`, bump a number
— and reruns your tests for each edit. If the tests still pass with the edit in place, that edit
**survived**: it's a change to your code's behaviour that **no test noticed**. Coverage says a line
*ran*; a survivor says a bug could live on that line and your suite would stay green.

A survivor is not a bug in your code and not a bug in gdmutant. It's a **gap in your tests** — a
specific, located "here's a change nothing caught."

## The summary, line by line

```
Mutation score: 60.0%
  killed:   6
  timeout:  0  (counted as killed)
  survived: 4
  ignored:  1  (suppressed, excluded from score)
  invalid:  0
  error:    0

Survivors (4):
  turn_order.gd:13:11  comparison  < -> <=
      → kill it: add a test at the boundary value where the two operators differ (e.g. equal inputs)
```

- **Mutation score** = killed ÷ (killed + survived). Timeouts count as kills (a mutation that hung the
  suite was caught); *ignored*, *invalid*, and *error* mutants are excluded from the score.
- Each survivor line is `path:line:column  operator  original -> replacement`, followed by a
  **`→ kill it:`** hint tailored to that operator — a concrete nudge toward the test that would catch it.

## Killing a survivor

Write (or strengthen) a test that **fails** under the survivor's change, then re-run — it should flip
to `killed`. The hint tells you the shape of that test; the general rule is *pin the exact behaviour
the edit moves*:

- **comparison** (`< -> <=`): test the **boundary** — the equal-inputs case where `<` and `<=` differ.
- **boolean** (`and -> or`): test a case where the operands **disagree** (one true, one false).
- **arithmetic / numeric**: assert the **exact** value or result, not just its sign or "nonzero".
- **constant / logical-not**: exercise the **branch** whose outcome the flip changes, both ways.
- **statement-deletion**: assert an effect that **disappears** when the statement is removed.

## Equivalent mutants (not every survivor is killable)

Sometimes a survivor **cannot** change observable behaviour — e.g. a clamp whose boundary is
unreachable, so `<` and `<=` give the same result on every possible input. That's an **equivalent
mutant**: a known, unavoidable limitation of mutation testing, not a tool bug. Chasing it with a test
is impossible by definition.

When you've **proven** a survivor is equivalent (or is benign and genuinely not worth a brittle test),
annotate its line so it becomes `Ignored` and drops out of the score:

- `# gdmutant: ignore` — suppress **every** mutant on the line.
- `# gdmutant: ignore[comparison]` — suppress only that operator's mutant(s) on the line (use the
  operator name from the report). Comma-list several: `ignore[comparison, numeric]`.
- Trailing text is the **reason**, surfaced in the report:
  `# gdmutant: ignore[comparison] equivalent — boundary unreachable`.

Only ignore *proven* equivalents, and always leave a reason — an `ignore` with no justification is
just a hidden coverage gap. See [`decisions/0004`](decisions/0004-equivalent-mutant-ignore-annotation.md)
and [`decisions/0006`](decisions/0006-operator-scoped-ignore-and-ignored-status.md) for the details.

## A healthy loop

Work down the survivor list: each one is either **killed** (you wrote the missing test) or **ignored**
(you proved it equivalent, with a reason). Both raise your confidence in the suite; neither leaves you
chasing a mutant forever. A perfect score isn't the goal — *understanding each survivor* is.
