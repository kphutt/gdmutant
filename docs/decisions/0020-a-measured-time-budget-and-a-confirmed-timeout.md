---
type: decision
status: active
created: 2026-09-25
---

# A measured per-mutant time budget, and a timeout that gets confirmed

## Status
Accepted. The per-mutant time budget is now built from measured parts instead of one multiplier on
the whole baseline, the worker-count multiplier is gone, and a mutant that runs past its budget is
re-run before it is recorded as a hang.

This partly reverses an earlier conclusion. [ADR-0019](0019-one-godot-per-mutant-stays.md) and
[ADR-0018](0018-coverage-analysis-stays-off-by-default.md) both measured the cost of a mutation run
on projects whose mutants do not hang, and on those projects the timeout budget never fires, so
tuning it looks like tuning a number nobody reads. A project with hanging mutants was measured
afterwards: eight timeouts accounted for four minutes of one six-minute-twenty-four run. On that
shape the budget is not a safety net, it is most of the run. The earlier conclusion was right about
the projects it had, and wrong as a general one.

## Context

### What the budget was
A mutant's budget was ten times the whole baseline suite's wall-clock, floored at ten seconds,
capped at ten minutes, and then multiplied again by the worker count under `--jobs N`.

Two things are wrong with that, and both are about which part of a run a mutation can actually
change.

It multiplied a fixed cost. A mutant can make your tests slower. It cannot make Godot boot
slower or your test framework start up slower, because those happen before any mutated code runs.
On the bundled corpus, a GdUnit4 baseline takes 1.378 s of which the report says 0.031 s was tests:
97.8% of what was being multiplied by ten is a fixed cost no mutation can touch. On a real game
project with 33 test files and 242 tests, the baseline takes 5.969 s of which 3.335 s was tests,
so 44% of it was still fixed cost. Multiplying that gave every mutant 59.7 s where the suite really
needs six.

The worker multiplier cancelled the parallelism exactly where it was needed. N hanging mutants
spread across W workers, each allowed W times the budget, take the same wall-clock as running them
one after another. That is why `--jobs 4` bought only 9.5% on a project where 13 to 24% of mutants
time out: the mutants that dominate the run were the ones the multiplier slowed back down.

The multiplier existed for a real reason, which was never measured: W workers contend for CPU and
RAM, so a genuinely passing suite could cross a serial budget under load and be recorded as a hang.
The reason is sound. The size was a guess, and the guess was off by an order of magnitude (below).

### Why the failure direction matters more than the number
A timeout counts as a kill. So a budget set too low turns a slow survivor into a false kill, which
raises the mutation score and hides the survivor: gdmutant's worst failure mode, a report that is
quietly wrong. Every instinct says to solve that by keeping the budget generous.

That instinct is what this ADR rejects. Stryker, PIT, Infection and mutant all wait out one
wall-clock budget and record a kill, with nothing verifying that the suite was really stuck. A
loose budget does not make that safe. It makes the same unverified guess behind a bigger number,
and it costs the whole run.

## What was measured
One machine, Windows 11, 16 cores, Godot 4.7-stable, gdmutant at `7e9e601`. Every project was
copied first and measured in the copy. Two fixtures: the bundled corpus (two suites, five tests)
and a private game project (33 test files, 242 tests, GdUnit4).

### Both frameworks report how long their tests took
The decomposition needs a test framework to say what part of a run was tests. It was not assumed.
Both were run for real and their JUnit XML read:

| Framework | `<testsuite>` carries `time`? | Example |
|---|---|---|
| GdUnit4 v6.1.3 | yes | `<testsuite id="0" name="test_independent" package="test" tests="2" time="0.009">` |
| GUT v9.7.1 | yes | `<testsuite name="gut_test/test_independent_gut.gd" tests="2" time="0.000187">` |

So the full form is what shipped, not the reduced one. Both also name the file a suite belongs to
in a way that reconstructs the string a test selection uses, which is what makes a per-file budget
possible at all:

* GUT writes the path under `res://` directly, inner class appended.
* GdUnit4 splits it: `name` is the file's stem and `package` is the directory below `res://`,
  nested directories included. Verified live against a test placed in `res://test/deep/inner`,
  which reported `name="test_nested" package="test/deep/inner"`. That is exactly the path
  GdUnit4's own `TESTSUITE_BEFORE` event reports as `resource_path()`, which is the string a
  selection names the file by.

Those two spellings live in the adapters, never in the engine, and three live tests
(`tests/test_selftest_live.py`) pin them against real runs of both frameworks. That matters more
than it looks: if the report's spelling and the selection's spelling ever diverge, per-file
budgeting silently falls back to whole-suite budgeting and every other test still passes. The
engine also says so out loud when it happens, rather than leaving a feature that does nothing.

### The fixed cost, and what contention really costs
The real game project, with the report's own test time subtracted to leave the fixed cost:

| Concurrent Godot processes | wall (min / median / max) | test time (median) | fixed cost (min / median / max) |
|---|---|---|---|
| 1 | 5.921 / 5.969 / 6.141 | 3.335 | 2.612 / 2.646 / 2.736 |
| 4 | 6.391 / 6.704 / 7.172 | 3.846 | 2.817 / 2.864 / 3.092 |
| 8 | 7.484 / 7.656 / 8.078 | 4.164 | 3.335 / 3.510 / 3.729 |

Eight concurrent processes make a healthy suite take 28% longer, not 700%. The fixed cost grows by
at most 1.12 s over its worst solo reading, and the test time by at most a factor of 1.354. The
bundled corpus, where the fixed cost is almost the whole run, shows the same shape smaller: 1.359
to 1.454 s alone, 1.453 to 1.625 s with four at once.

## Decision

### The formula
```
budget = 2.0 x netTime + 8 s + measuredOverhead
```
floored at 10 s, capped at 600 s, and taking no worker count at all.

* `netTime` is what the baseline's own report says its tests took. With
  `--coverage-analysis per-file`, it is the summed time of the test files this mutant will actually
  run. With selection off, or for a test file the report never named, it is the whole suite's test
  time. One unknown file falls back for the whole set: adding up the files that *are* known and
  skipping the rest would budget a mutant for part of its run while looking like a measurement.
* `measuredOverhead` is the baseline's wall-clock minus `netTime`, recorded once, free, at the
  baseline that had to run anyway. It is the framework's startup and the engine's boot, per project
  and per framework, and it is added back unmultiplied.
* The constant absorbs variance and contention.

### How the numbers were chosen
Stryker uses factor 1.5 with a 5000 ms constant and PIT uses 1.25 with 4000 ms. Those were not
copied. The decomposition transfers, the numbers do not, because Godot's startup variance on
Windows is worse than either runtime's.

Factor 2.0. The factor multiplies test time, so it has to cover every way test time can grow
without the suite being broken. Measured worst case under eight concurrent processes: the reported
test time went from 3.263 s to 4.418 s, a factor of 1.354. A factor of 2.0 covers that with roughly
48% of the budget left over for a mutant that legitimately runs slower without failing. It is
deliberately looser than both prior-art tools, for the reason in "Why the failure direction
matters".

Constant 8 s. The constant carries the contention allowance, which is what the worker
multiplier used to do badly. Measured worst case: the fixed cost ran 1.12 s above its worst solo
reading with eight suites at once. 8 s is about seven times that, which leaves room for a machine
with fewer cores than this one, a cold asset cache, or a virus scanner arriving at the wrong
moment. It is also close to Stryker's 5 s, arrived at from a different direction.

Floor 10 s, cap 600 s. Both unchanged. On a suite fast enough that the floor is the whole
budget, the floor is already many times the real run.

### A timeout is confirmed, not assumed
A tight budget alone would be the same unverified guess as everyone else's, just in the dangerous
direction. So it is not alone.

When a mutant runs past its budget, it is re-run once, on its own, under a much larger
budget: `10.0 x netTime + 8 s + measuredOverhead`. If it finishes, it was never hanging, and its real
verdict is recorded. Only the mutants that ran long pay for this.

The confirmation factor of 10.0 is the old whole-wall-clock factor, now applied only to the part a
mutant can make slower. That choice is what keeps the arithmetic honest for a genuine infinite
loop, which pays both budgets. On the real game project: the old budget was 59.7 s, the new first
budget is 17.3 s and the confirmation budget 44.0 s, so a genuine hang costs 61.3 s against the old
59.7 s serially, and 61.3 s against the old 238.8 s at `--jobs 4`. Serially a real hang is 2.7%
more expensive. Everything else gets much cheaper, and the false kill stops being possible to
begin with.

There is no confirmation in three cases, each meaning a second run could say nothing the first did
not: an explicit `--timeout` (the user named a number), a baseline whose report gave no durations
(nothing to loosen), and a budget already sitting on the floor or the cap (it cannot grow).

### The run says which kind of timeout it had
`reprieved` counts the mutants that ran past the first budget and then finished. Every one of them
is a kill a tool taking the first budget at its word would have handed you. `confirmed_timeouts`
counts the hangs a second, longer run agreed were hangs. A run with timeouts always states how many
were confirmed, including when the answer is none, because silence there would read as
confirmation.

That count is also this design's own tripwire. If the budget is ever set too tight, `reprieved`
climbs and says so in the summary, instead of the score quietly going up.

### What was considered and left out
Raising the first budget as a run learns. The measurement below shows six mutants on one real
file that legitimately needed three to seven times the baseline, which is what the confirmation
pass is paying for. A run could notice its first reprieve and raise the first budget for every
mutant after it, so the cost is paid once instead of per slow mutant. It was left out because it
makes the budget depend on the order mutants happen to run in, and this engine's verdicts are
deterministic and reproducible on purpose (a CI check can trust them). A budget that differs
between two runs of the same project is a verdict that can differ between them. Worth revisiting
if the confirmation cost shows up as a real complaint, and it would need a way to stay
reproducible.

Watching the coverage markers to detect a hang instead of waiting it out. A mutant that is
genuinely stuck stops firing markers, which is a live signal no production mutation tester has.
Two things rule it out today, and neither is a guess. The recorder writes its hits file only at
`NOTIFICATION_PREDELETE`, at process teardown, so a hung process writes nothing at all and there is
no partial file to watch. And markers are only placed in the throwaway copy the coverage pass runs
in: a per-mutant run uses an unmarked project, so there is no marker stream to watch even in
principle without marking every worker copy and paying the marker cost on every single run. This
needs a different channel, not a different reader of the same one, so it is not being built on a
guess.

### The check that decided this
Verdicts, not the score. The same mutants were run twice on the same project copy layout, once
under the old budget and once under the new one, and every mutant's verdict compared. The rule: a
mutant that was `Survived` must not become `Timeout`, because that is the false kill this whole
design is built to avoid. The old budget was reproduced exactly by passing it as an explicit
`--timeout`, which fixes the budget and turns the confirmation off, which is what the old code did.

PLACEHOLDER_EVIDENCE_TABLE

## Consequences
* On a project whose mutants do not hang, almost nothing changes: the budget never fires, so the
  verdicts and the wall-clock are the same. The corpus verdict diff confirms it exactly.
* On a project whose mutants do hang, the run gets much shorter, and `--jobs N` starts paying off
  on the mutants it never used to.
* A genuine infinite loop costs slightly more serially, because it pays the first budget and then
  the confirmation budget. That is the price of the false kill becoming impossible, and it is 2.7%
  on the project measured.
* `--timeout` now means something stricter than it did: it fixes the budget and turns the
  confirmation off.
* `ReportedSuite` gained `time` and `file`. `file` is filled by the runner, never the engine, so
  the engine still hands test-file strings back without ever parsing one (NF-3).

## When to revisit
* A framework whose report gives durations that are not comparable to wall-clock (tests that run in
  parallel inside the framework, say) would make `measuredOverhead` clamp to zero. The clamp is
  already there and fails toward a bigger budget, but the decomposition would stop earning its
  keep.
* A `reprieved` count that is routinely nonzero on healthy projects means the factor or the
  constant is too small for that shape of project, and the number to raise is the constant.
* If the recorder ever writes incrementally, or a second channel appears that a running mutant can
  be watched on, the marker-based hang detection above becomes worth measuring.
