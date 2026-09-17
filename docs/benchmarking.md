---
type: how-to
status: active
created: 2026-09-17
---

# Benchmarking gdmutant

`scripts/benchmark.py` measures how long gdmutant itself takes per mutant, and keeps a history so
that cost can be watched as a trend. It is for contributors changing the engine, not for users.

## What kind of test this is

Not a load test in the usual sense. A load test sends many users or requests at a running service
at once, and gdmutant is a command-line tool that runs one batch. This is three related things:

- A benchmark: the same fixed work, timed repeatably.
- A scalability test: the same work at growing sizes (`--sizes`), to show how cost grows with the
  input. A cost per mutant that climbs as the file gets bigger is the thing to look for.
- A regression check: today's numbers against a saved baseline (`--compare`), with every run
  recorded (`--record`) so a slow drift shows up in `--trend`.

## What it measures, and what it leaves out

A real run spends most of its time inside Godot, running your tests once per mutant. That time is
the project's, not gdmutant's. The engine scenarios replace Godot with an instant fake test runner,
so what remains is gdmutant's own work: parsing, finding mutation sites, re-parsing each mutant to
reject invalid GDScript, writing and restoring files, copying the project for `--jobs`, and
rendering reports.

| Scenario | What is timed |
|---|---|
| `generate` | parsing a file and listing its mutants |
| `apply` | applying every mutant and re-parsing the result |
| `run` | the full engine loop, one mutant at a time |
| `run-jobs4` | the same loop with four workers, including their project copies |
| `run-files` | one multi-file run over every workload, the way a directory run works |
| `report` | rendering the JSON and HTML reports |
| `godot-corpus` | the realistic end to end run with a real Godot, opt in with `--godot PATH` |

The workloads are `corpus/turn_order.gd`, the real fixture the live self-tests use, and generated
files of N functions (`synthetic-N`) that use every operator. The generator is deterministic, so
the same N is the same file everywhere.

Every result also records how many mutants it worked on and how many were killed. Those counts are
what make two runs comparable: if they differ, the two runs did different work.

## Running it

From the repo root:

```sh
uv run python scripts/benchmark.py                      # a table, default sizes 2 and 8
uv run python scripts/benchmark.py --record             # also add the run to the history
uv run python scripts/benchmark.py --trend              # show the history as trends
uv run python scripts/benchmark.py --sizes 2,8,32       # a bigger size sweep
uv run python scripts/benchmark.py --scenario run --scenario apply
uv run python scripts/benchmark.py --scenario godot-corpus --godot "$(mise which godot)"
```

The history lives in `.benchmarks/history.jsonl`, which git ignores, because timings belong to one
machine. `--trend` shows only the runs recorded on a machine and Python matching the current one,
and says how many it left out.

## Comparing before and after a change

```sh
uv run python scripts/benchmark.py --json before.json   # on the commit before the change
uv run python scripts/benchmark.py --json after.json --compare before.json
```

`--compare` exits 0 when nothing got slower past the tolerance, 1 when something did, and 2 when
the two runs did not do the same work. A scenario missing from either side, or a different mutant
or killed count, is exit 2, never a pass. A slowdown counts only when the median grew by more than
`--tolerance` (25% by default) and by more than `--min-delta` seconds (0.005 by default), so timer
noise on a tiny number is not reported as a regression.

## Getting numbers worth comparing

- Compare runs from the same machine and Python. The JSON records both, and `--compare` notes a
  mismatch.
- Keep the machine otherwise idle. A game, a build or a sync in the background moves the numbers
  more than most code changes do.
- For a change that matters, alternate: run before, after, before, after, and look at whether the
  difference holds every time. One pair of runs can mislead.
- The table's `per mutant` column is the median divided by the mutant count. Watch it across
  `--sizes`: flat means the cost grows with the number of mutants, climbing means it grows faster.

## What the tests check

`tests/test_benchmark.py` checks the work, never the time, because a test that fails on a slow
machine teaches people to ignore it. It pins the mutant and killed counts of each scenario, that
the four-worker run does exactly the serial run's work, that the fake runner reads a worker's copy
rather than the original project, and every exit code of `--compare`. The real Godot scenario is
checked in `tests/test_selftest_live.py`, which runs only with `GDMUTANT_GODOT` set.
