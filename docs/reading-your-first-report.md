---
type: explanation
status: active
created: 2026-07-18
---

# Reading your first gdmutant report

You ran `gdmutant run …` and got a list of **survivors**. This is a short, human-facing guide to what
that means and what to do next. (Driving gdmutant from an AI agent instead? See
[`agent-guide.md`](agent-guide.md).)

## What a survivor is

gdmutant changes your source one tiny edit at a time — flip `>` to `>=`, `and` to `or`, bump a number
— and reruns your tests for each edit. If the tests still pass, that edit **survived**: a change to
your code's behaviour that **no test noticed**. Coverage says a line *ran*; a survivor says a bug
could live on that line and your suite would stay green.

A survivor isn't a bug in your code or in gdmutant — it's a **gap in your tests**, a specific, located
"here's a change nothing caught."

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

──── survived ──────────────────────────────────────────── comparison ────

  turn_order.gd:13   func clamp_initiative

     13 |     if value < 0:
        |              ^  changed  <  to  <= — every test still passed

  gap    Your tests pass whether this says `<` or `<=`. They run this
         line, but never the one input where the two disagree — equal
         operands. That case is untested.

  risk   Passing here is false confidence, not proof. A later refactor or
         merge that changes the equal case slips through green. If the
         equal case has a right answer, no test guards it.

  start  Add a test that reaches this line with two equal operands (a
         value compared to itself) and assert the result you expect. Only
         you know that result — gdmutant reports the gap, not it.

  more   https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md#comparison
──────────────────────────────────────────────────────────────────────────
```

- **Mutation score** = detected ÷ (detected + survived), where **detected = killed + timeouts** (a
  mutation that hung the suite was caught, so a timeout counts as a kill); *ignored*, *invalid*, and
  *error* mutants are excluded from the score entirely.
- Each survivor is a block: the **code line** with a caret on the changed token, then **`gap`** (what
  your tests don't check), **`risk`** (why that matters), **`start`** (the input to add — gdmutant
  names the gap, never the expected answer), and **`more`** (a deep-link to the per-operator section
  of the survivor reference). One block per survivor; the four here are shown as one for brevity.

## Killing a survivor

Write (or strengthen) a test that **fails** under the survivor's change, then re-run — it should flip
to `killed`. The `start` line gives the shape; the rule is *pin the exact behaviour the edit moves*
(for a comparison flip, test the equal-inputs boundary; for `and`/`or`, the case where the operands
disagree). Each operator's `more` link opens that operator's section in the
[survivor reference](survivors/README.md) with the exact test to add for that mutation.

## Equivalent mutants (not every survivor is killable)

Sometimes a survivor **cannot** change observable behaviour — e.g. a clamp whose boundary is
unreachable, so `<` and `<=` give the same result on every possible input. That's an **equivalent
mutant**: a known, unavoidable limitation of mutation testing, not a tool bug, and impossible to chase
with a test by definition.

When you've **proven** a survivor is equivalent (or is benign and genuinely not worth a brittle test),
annotate its line with `# gdmutant: ignore` so it becomes `Ignored` and drops out of the score. Add
an operator name to scope it (`# gdmutant: ignore[comparison]`) and trailing text for the **reason**,
which the report surfaces: `# gdmutant: ignore[comparison] equivalent — boundary unreachable`.

Only ignore *proven* equivalents, and always leave a reason — an `ignore` with no justification is
just a hidden coverage gap. The [agent guide](agent-guide.md#the-survivor--killing-test-loop) has the
full annotation syntax and a worked example;
[`decisions/0004`](decisions/0004-equivalent-mutant-ignore-annotation.md) and
[`0006`](decisions/0006-operator-scoped-ignore-and-ignored-status.md) record the design.

## A healthy loop

Work down the survivor list: each one is either **killed** (you wrote the missing test) or **ignored**
(you proved it equivalent, with a reason). Both raise your confidence in the suite; neither leaves you
chasing a mutant forever. A perfect score isn't the goal — *understanding each survivor* is.

## Viewing the report

`--html report.html` writes a self-contained, source-highlighted page you can open directly — the
easiest survivor-by-survivor view. `--json` emits the standard
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema, so
the same report also renders in the Stryker Dashboard or any host of that viewer.
