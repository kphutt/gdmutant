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

  more   https://github.com/kphutt/gdmutant/blob/main/docs/survivors/comparison.md
──────────────────────────────────────────────────────────────────────────
```

- **Mutation score** = detected ÷ (detected + survived), where **detected = killed + timeouts** (a
  mutation that hung the suite was caught, so a timeout counts as a kill); *ignored*, *invalid*, and
  *error* mutants are excluded from the score entirely.
- Each survivor is a block: the **code line** with a caret on the changed token, then **`gap`** (what
  your tests don't check), **`risk`** (why that matters), **`start`** (the input to add — gdmutant
  names the gap, never the expected answer), and **`more`** (a per-operator explainer page). One
  block per survivor; the four here are shown as one for brevity.

## Killing a survivor

Write (or strengthen) a test that **fails** under the survivor's change, then re-run — it should flip
to `killed`. The `start` line tells you the shape of that test; the general rule is *pin the exact
behaviour the edit moves*:

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

## Wiring a viewer yourself

`--html report.html` gives you a self-contained page, and `--json` renders in the Stryker Dashboard.
If you'd rather keep the report and the page separate, the `--json` output is the standard
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema,
so any host of that viewer works. Save this next to your `report.json` as `view.html`:

```html
<mutation-test-report-app></mutation-test-report-app>
<script src="https://www.unpkg.com/mutation-testing-elements@3.8.4"></script>
<script>
  fetch("report.json")
    .then((r) => r.json())
    .then((report) => (document.querySelector("mutation-test-report-app").report = report));
</script>
```

then serve the folder and open it (`python3 -m http.server` → visit `view.html`) for a
source-highlighted, survivor-by-survivor view.
