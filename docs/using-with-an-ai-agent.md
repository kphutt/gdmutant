---
type: how-to
status: active
created: 2026-07-11
---

# Driving gdmutant from an AI agent

A one-read guide for an AI agent to run gdmutant and act on the results correctly: how to invoke
the CLI and read its output. This is a *how-to-use-the-tool* guide for consumers, distinct from
[`AGENTS.md`](../AGENTS.md), which tells a contributor how to work *on* gdmutant's own source, not
how to run it.

## Install

gdmutant is a Python CLI (Python 3.12+):

```sh
pip install gdmutant
```

With uv, pin the interpreter explicitly, because `uv init` alone floors on whatever it finds:

```sh
uv init --python 3.12
uv add gdmutant
```

Then `uv run gdmutant …`. The README's [Quickstart](../README.md#quickstart) has the full setup.

## Invoke

```sh
gdmutant run <file.gd> --project <godot-project-dir> --json -
```

- `--json -` streams the machine-readable report to stdout. The human summary and per-mutant
  progress go to stderr. Capture stdout for parsing. Stdout stays pure JSON.
- `--dry-run` lists the mutants gdmutant *would* generate, without Godot and without running any
  tests: a fast, dependency-free preview.
- `--require-clean` refuses to run unless git holds a copy of the source file it could put
  back (exit 2). Uncommitted changes fail it, and so does anything gdmutant could not
  confirm: a file git ignores, a file outside any repository, or a machine with no git.
  Without it, gdmutant only *warns* and proceeds (it never blocks on a prompt: safe for headless
  agents). `--no-require-clean` is the other half of that switch: it turns the refusal back off for
  one run, which is what you need when a `.gdmutant.toml` sets `require-clean = true` and this
  particular run has to go ahead on a dirty tree anyway.
- `--report step-summary` writes the surviving mutants and their explanations as Markdown to the
  GitHub Actions job summary (the file named by `$GITHUB_STEP_SUMMARY`), so survivors show up in
  the run summary a reviewer already opens. With that variable unset it prints the same Markdown to
  stdout, so don't combine it with `--json -` unless you want both on one stream. The flag is
  repeatable and advisory: a failed write warns and changes neither the score nor the exit code.
- `--since <ref>` mutates only the lines changed since a git ref (e.g. `--since origin/main`), the
  fast per-PR mode for CI. `--jobs N` evaluates mutants in parallel.
- Other flags: `--tests res://test`, `--godot <path>`, `--report-path <rel>`, `--timeout <seconds>`.
  `gdmutant run --help` lists them all.
- Runner selection. `--runner gdunit4` (default) and `--runner gut` are first-class peer JUnit
  adapters (per-test detail). No JUnit XML? `--runner command --command "<test cmd>"` takes any
  command that exits non-zero on failure (e.g. a hand-rolled
  `godot --headless --script res://tests/run_tests.gd`). See
  [`docs/decisions/0011`](decisions/0011-runner-agnostic-adapter-seam.md) (the runner seam) and
  [`docs/decisions/0005`](decisions/0005-exit-code-test-runner-convention.md) (the exit-code fallback).
  Under `--runner command` the executable comes from the `--command` string itself. `--godot` is
  not read in that mode, so put the full path inside `--command`.
- Import the project once before the first run. On a checkout Godot has never opened, it imports
  every asset before it runs anything, which on a real game is minutes with no output at all, long
  enough to look like a hang and get killed. `--runner gdunit4` / `--runner gut` do this warm-up
  themselves. `--runner command` cannot, and prints a note when the project has no `.godot/`
  directory. Run `godot --headless --path <project> --import` once in setup.
- Nothing predicts a finish time. Before the run gdmutant states the mutant count and the
  per-mutant cap. During it, a heartbeat reports what has finished. After it, one line gives the
  wall-clock with the timeout cost broken out. Parse the closing `Done in ...` line for the real
  duration. Do not try to reconstruct a schedule from the opening one. `--progress plain` (the
  automatic choice off a TTY and under `CI=true`) heartbeats every 60s or 10% of mutants, whichever
  is rarer. `--progress none` silences it while keeping the plan and closing lines.

## Exit codes (the contract)

- `0`: the run completed. Survivors are normal output, not a failure. Parse them.
- `1`: the unmutated *baseline* suite failed. Fix your tests first. Mutation-testing a red
  suite is meaningless.
- `2`: a setup/input error. The source is unreadable or not valid GDScript, `--project`
  doesn't exist, `--require-clean` was set on a dirty tree, the test-runner executable (`godot`)
  wasn't found, or the report couldn't be written. The stderr message says which.

## Safety guarantee

gdmutant mutates the source file in place, then restores it to its original bytes in a
`finally`, after every mutant and on a normal exit or Ctrl-C. Your working tree is returned
unchanged. The only way a swap can persist is a hard kill (SIGKILL / power loss), so commit or
stash first, or pass `--require-clean`.

## Output schema (Stryker `mutation-testing-elements`, v2)

`--json -` emits one report object:

```json
{
  "schemaVersion": "2",
  "thresholds": {"high": 80, "low": 60},
  "files": {
    "corpus/turn_order.gd": {
      "language": "gdscript",
      "source": "<full file source>",
      "mutants": [
        {
          "id": "0",
          "mutatorName": "comparison",
          "replacement": ">=",
          "location": {"start": {"line": 8, "column": 17}, "end": {"line": 8, "column": 18}},
          "status": "Survived"
        }
      ]
    }
  }
}
```

- `status` is one of `Killed`, `Survived`, `Timeout` (the mutation hung the suite: a detection, so
  it counts as killed), `Ignored` (a `# gdmutant: ignore` annotation suppressed it, excluded
  from the score. Its reason, if any, is in `statusReason`), `CompileError` (the mutant didn't
  parse, never counted as killed), or `RuntimeError` (the runner failed to execute it, e.g. a
  Godot crash).
- Locations are 1-based. The `end` `column` is exclusive.
- Actionable survivors are the mutants with `"status": "Survived"`. Those are the gaps a test
  should close.
- Mutant order is deterministic (fixed generation order), so `id`s and the survivor list are
  stable across runs and safe to diff between attempts.

## The survivor → killing-test loop

Same loop the [README's workflow section](../README.md#the-workflow) walks a human through
(pick a target, kill or annotate every survivor, re-run, move on), driven from JSON instead of the
console:

1. Run with `--json -`, capture stdout, and read `files[<path>].mutants`.
2. For each `"Survived"` mutant: it gives you a `location` and the `replacement` (the exact change
   no test caught). Write or strengthen a test that *fails* under that change, usually an
   assertion pinned to the boundary or value the mutation moves.
3. Re-run and confirm that mutant is now `"Killed"`.
4. If a survivor is a genuine equivalent mutant, one that *cannot* change observable behavior
   (e.g. a clamp whose boundary can't be reached), annotate its line so it becomes `Ignored`
   (excluded from the score):
   - `# gdmutant: ignore`: suppress every mutant on the line.
   - `# gdmutant: ignore[comparison]`: suppress only that operator's mutant(s) on the line, when a
     killed or timeout mutant shares the line (use the `mutatorName` from the report). Comma-list
     several: `ignore[comparison, numeric]`.
   - Trailing text is the reason (surfaced as `statusReason`): `# gdmutant: ignore[comparison]
     equivalent at the boundary`.

   Only suppress *proven* equivalents (or benign, brittle-to-kill mutants, with a reason). See
   [`docs/decisions/0004`](decisions/0004-equivalent-mutant-ignore-annotation.md) and
   [`0006`](decisions/0006-operator-scoped-ignore-and-ignored-status.md).
5. A survivor inside an `assert` is step 4's commonest case, and it needs no analysis to
   recognise: a failed `assert` aborts the Godot process, so no in-process test can pass on the
   original and fail on the mutant. Its `description` says so, and `gdmutant` counts them under the
   survivor list. Do not try to write a test for one. Annotate the line or leave it. Full
   reasoning: [the assert section](survivors/README.md#assert).

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

### A killable survivor

The numeric mutant `0 -> -1` on `if value < 0` survives: at `value == -3`
both the original and the mutant return `0`, so the existing tests can't tell them apart. At
`value == -1` they differ: the original clamps to `0`, the mutant returns `-1`. The boundary test
kills it:

```gdscript
func test_clamp_initiative_lower_boundary() -> void:
	assert_int(TurnOrder.clamp_initiative(-1, 10)).is_equal(0)  # kills `< 0` -> `< -1`
```

### A genuine equivalent

The comparison mutant `> -> >=` on the *upper* clamp
(`if value >= max_value`) can never be caught: at `value == max_value` the original falls through to
`return value` (which is `max_value`) while the mutant returns `max_value` directly. Every other
input already agrees. Suppress it with a reason:

```gdscript
	if value > max_value:  # gdmutant: ignore  (>= is equivalent: value == max_value returns max either way)
		return max_value
```

Beware near-misses: the *lower* clamp's `< -> <=` looks equivalent but isn't. At
`value == 0, max_value == -1` the original returns `-1` while `<= 0` returns `0`, so a test kills it.
Suppress only a mutant you have *proven* equivalent across all inputs. If you're unsure whether a
mutant is killable, it usually is. Write the test.
