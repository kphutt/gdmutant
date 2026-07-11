---
type: guide
status: active
created: 2026-07-11
---

# Recipe: turning survivors into killing-test PRs

A worked, copy-pasteable workflow for an agent (or a person) driving gdmutant to *close* the gaps it
finds. It expands the short loop in [`agent-guide.md`](agent-guide.md) with a real example. The
whole loop is designed to **terminate**: every survivor ends up either killed by a new test or
suppressed as a genuine equivalent.

## The loop

1. **Run** gdmutant on the target module and capture the machine-readable report:
   ```sh
   gdmutant run corpus/turn_order.gd --project corpus --json - > report.json
   ```
   (`--json -` writes the report to stdout; the human summary + progress go to stderr — see the
   agent guide.)
2. **List the survivors** — the mutants with `"status": "Survived"` in `report.json`. Each carries a
   `location` (1-based `line`/`column`) and the `replacement` (the change no test objected to).
3. **For each survivor, decide: killable or equivalent?**
   - *Killable* → write the smallest test that **fails** under that `replacement` (usually an
     assertion pinned to the exact value/boundary the mutation moves), then go to step 4.
   - *Equivalent* (no input can make the `replacement` observable) → suppress it (step 5).
4. **Re-run and confirm the kill.** Run gdmutant again; the mutant's status should now be `Killed`.
   If it still survives, the test doesn't actually exercise that line — strengthen it.
5. **Suppress genuine equivalents** by marking the line with `# gdmutant: ignore` so it stops being
   reported (see [`docs/decisions/0004`](decisions/0004-equivalent-mutant-ignore-annotation.md)).
   Reserve this for mutants you've *proven* can't change behavior — not for ones that are merely
   hard to kill.
6. **Open a PR** with the new killing tests (and any `# gdmutant: ignore` annotations). Because the
   deterministic operator core is reproducible, a CI re-run reproduces the same verdicts, so the PR
   is safe to gate a human review on.

Repeat until no `Survived` mutants remain. The loop terminates because step 3 forces every survivor
down one of two paths — killed or suppressed — with no "try again forever" branch.

## Worked example (the bundled corpus)

`corpus/turn_order.gd` clamps an initiative value into `[0, max_value]`:

```gdscript
static func clamp_initiative(value: int, max_value: int) -> int:
	if value < 0:
		return 0
	if value > max_value:
		return max_value
	return value
```

The corpus suite tests `clamp_initiative(-3, 10) == 0`, `(12, 10) == 10`, `(6, 10) == 6`.

**A killable survivor.** The numeric mutant `0 -> -1` on `if value < 0` (turning it into
`if value < -1`) *survives*: at `value == -3` both the original and the mutant return `0`, so the
existing test can't tell them apart. But at `value == -1` they differ — the original clamps to `0`,
the mutant returns `-1`. Add the boundary test:

```gdscript
func test_clamp_initiative_lower_boundary() -> void:
	assert_int(TurnOrder.clamp_initiative(-1, 10)).is_equal(0)  # kills `< 0` -> `< -1`
```

Re-run: the mutant is now `Killed`.

**A genuine equivalent.** The comparison mutant `< -> <=` on the *same* line (`if value <= 0`) can
**never** be caught: at `value == 0` the original falls through and returns `value` (which is `0`),
and the mutant returns `0` directly — identical for every input. No test can kill it, so suppress it:

```gdscript
	if value < 0:  # gdmutant: ignore  (<= is equivalent here: value is 0 at the boundary)
		return 0
```

Now the survivor set an agent sees is *actionable*: everything left is a real gap to close.

## Tips

- **Batch per file.** Run one module at a time and fix its whole survivor list before moving on;
  the report is per-file.
- **Trust the exit code.** `0` means the run completed (survivors and all) — parse the report;
  `1` means the baseline suite is red (fix that first); `2` is a setup/input error. See the
  [agent guide](agent-guide.md).
- **Boundaries first.** Most survivors are comparison/off-by-one mutants; a test at the exact
  boundary value kills them and is the highest-signal test to add anyway.
- **Don't over-suppress.** `# gdmutant: ignore` is for *proven* equivalents. If you're unsure
  whether a mutant is killable, it usually is — write the test.
