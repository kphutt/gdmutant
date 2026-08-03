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
  progress go to stderr. Capture stdout for parsing. One flag ignores `--json` and writes its own
  text to stdout instead: [Keeping stdout parseable](#keeping-stdout-parseable) has it.
- `--dry-run` lists the mutants gdmutant *would* generate, without Godot and without running any
  tests: a fast, dependency-free preview. It ignores `--json` and prints its list as plain text.
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
  stdout instead, so `> summary.md` works locally. That fallback is the one thing `--json -` cannot
  live beside: two documents on one pipe leaves neither readable, so gdmutant refuses the
  combination up front (exit 2) rather than interleaving them. Set `$GITHUB_STEP_SUMMARY` to a file
  and both work together, which is what the GitHub Action does on every run. `step-summary` is the
  only value it takes, and repeating the flag changes nothing. It is advisory: a failed write warns
  and changes neither the score nor the exit code.
- `--since <ref>` mutates only the lines changed since a git ref (e.g. `--since origin/main`), the
  fast per-PR mode for CI. When no line in the given paths changed since that ref, gdmutant runs no
  tests at all and exits 0, with a note on stderr. It still writes the report you asked for, and
  emits the job summary if you asked for one: every given file is there with an empty `mutants`
  list, and no score is reported, exactly as for any other run with nothing to score, since the
  report carries no score key in either case. So `--json -` stays parseable and needs no special
  case. A PR that touches no `.gd` file hits this every time.
- `--jobs N` evaluates mutants in parallel, each worker inside its own copy of the project. That
  copy is what keeps your own file untouched (see [Safety guarantee](#safety-guarantee)), and it
  brings one restriction: every file to mutate has to sit inside `--project`, or the run exits 2.
  That bites whenever an explicit `--project` names a directory that does not contain the sources.
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
  every asset before it runs anything, which on a real project is minutes with no output at all,
  long enough to look like a hang and get killed. `--runner gdunit4` / `--runner gut` do this warm-up
  themselves. `--runner command` cannot, and prints a note when the project has no `.godot/`
  directory. Run `godot --headless --path <project> --import` once in setup.
- Nothing predicts a finish time. Before the run gdmutant states the mutant count and the
  per-mutant cap. During it, a heartbeat reports what has finished. After it, one line gives the
  wall-clock with the timeout cost broken out. Parse the closing `Done in ...` line for the real
  duration. Do not try to reconstruct a schedule from the opening one. `--progress plain` (the
  automatic choice off a TTY and when `CI=true` or `CONTINUOUS_INTEGRATION=true`) heartbeats every
  60s or 10% of mutants, whichever is rarer. `--progress none` turns the whole progress stream off:
  no heartbeat, no plan line, no closing line, no line per mutant, and none of the `preparing the
  project` / `running the unmutated (baseline) suite` notices either. That bounds the stderr volume
  instead of trimming it, and the price is real: you lose the `Done in ...` line, and a first run on
  a fresh checkout is silent for however long Godot takes to import. Use `--progress plain` when a
  script wants the duration or the run is long enough that silence looks like a hang.

## Keeping stdout parseable

With `--json -` the report is the only thing on stdout. No flag can quietly append to it:

- `--html <path>` is safe to combine. The report goes to stdout, the page goes to the file, and the
  `Wrote HTML report to ...` note goes to stderr with the rest of the human text.
- `--report step-summary` is safe whenever `$GITHUB_STEP_SUMMARY` is set, which is always true on
  GitHub Actions. Unset, its Markdown would fall back to stdout, and gdmutant exits 2 with a message
  naming both flags rather than mixing two documents into one stream.
- `--dry-run` is the one case where stdout is not the report. It ignores `--json` and prints its
  plain-text mutant list there, and says on stderr that it is doing so. Do not pair it with a run
  you parse.

`--json <path>`, a real path rather than `-`, sidesteps the question entirely: the report goes to
that file and stdout carries only human text.

## Project config (`.gdmutant.toml`)

gdmutant reads `.gdmutant.toml` from the directory it runs in, and its keys seed the defaults of the
flags of the same name (`project`, `runner`, `command`, `godot`, `tests`, `report-path`, `timeout`,
`require-clean`, `exclude`). An explicit flag still wins.

Two of those keys name a program gdmutant would execute: `command` and `godot`. In a checkout you
did not write, that file is somebody else's instruction about what runs on your machine, so
gdmutant refuses to act on those two and exits 2 instead. The refusal fires on any run, `--dry-run`
included, and it is the exit-2 cause most likely to surprise an agent working in a directory it did
not create. Two ways past it:

- `--trust-config` acts on the file's `command` and `godot`. Use it only where the checkout is
  trusted.
- Pass the value yourself (`--command ...`, `--godot ...`). An explicit flag wins over the file and
  needs no trust, because the program is then one you named.

Every other key is read normally: none of them can decide what gets executed.

## Exit codes (the contract)

- `0`: the run completed. Survivors are normal output, not a failure. Parse them. Every exit-0 path
  except `--dry-run` writes the report you asked for, including `--since <ref>` with no changed
  lines: that one is an empty report, not an empty stdout. `--dry-run` writes no `--json`/`--html`
  report at all, says so on stderr, and prints its mutant list to stdout instead.
- `1`: the unmutated *baseline* suite failed. Fix your tests first. Mutation-testing a red
  suite is meaningless.
- `2`: a setup or input error. The stderr message says which one. The causes:
  - the source is unreadable or not valid GDScript, or no given path holds a parseable `.gd` file
  - `--project` does not name an existing directory
  - `--require-clean` was set and gdmutant could not confirm git holds a copy of the source: a
    dirty tree, a file git ignores, a file outside any repository, or a machine with no git
  - `.gdmutant.toml` sets `command` or `godot` and `--trust-config` was not passed, or that file
    cannot be read or holds a value of the wrong type
  - the test-runner executable was not found: `godot`, or the program named by `--command`
  - `--runner command` with no `--command`, or a `--command` string that cannot be split into words
  - `--since` names a ref git cannot diff against
  - `--jobs` above 1 with a source file that does not sit inside `--project`
  - `--jobs` below 1
  - `--json -` and `--report step-summary` together with `$GITHUB_STEP_SUMMARY` unset: two
    documents, one stdout
  - a report file could not be written, or the source file could not be rewritten or put back

## Safety guarantee

What gdmutant does to the file you point it at depends on `--jobs`.

With `--jobs 1`, the default, gdmutant mutates the source file in place and writes the original back
in a `finally`, after every mutant and on a normal exit or Ctrl-C.

With `--jobs` above 1, each worker copies the whole project and mutates the file inside its own
copy. The file you point at is never written at all, so nothing in your working tree can change and
`--require-clean` guards against a risk this mode does not carry.

A serial run has two ways to leave a mutant on disk:

- A hard kill (SIGKILL, power loss) between the mutating write and the restore.
- A failed restore. gdmutant writes through a temporary file and a rename, and keeps no degraded
  fallback: when that staged write cannot be made (no room for the temporary file, a failed flush
  to disk, a Windows lock that outlasts its retries) it refuses rather than write in a way that
  could truncate the file. On the *restore* write, that refusal ends the run with exit 2 and leaves
  the mutant in your file. No crash is involved.

So on a serial run, commit or stash first, or pass `--require-clean`. Treat exit 2 as a reason to
inspect the source file rather than only to retry: the message names the path gdmutant could not
write, and `git checkout -- <path>` puts the original back.

The restore is byte-exact for a file whose lines all end the same way. gdmutant rewrites the file
with CRLF when the original holds any CRLF at all, and with LF otherwise, so a file that mixes the
two comes back entirely CRLF.

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
          "status": "Survived",
          "description": "Your tests pass whether this says `>` or `>=` ...",
          "statusReason": "Passing here is false confidence, not proof ... Add a test that ..."
        }
      ]
    }
  }
}
```

- `status` is one of `Killed`, `Survived`, `Timeout` (the mutation hung the suite: a detection, so
  it counts as killed), `Ignored` (a `# gdmutant: ignore` annotation suppressed it, excluded
  from the score), `CompileError` (the mutant didn't parse, never counted as killed), or
  `RuntimeError` (the runner failed to execute it, e.g. a Godot crash).
- Every `Survived` mutant carries two prose fields, and they are the reason to read this report
  rather than only its locations: `description` states the gap, what no test pins, and
  `statusReason` states why that matters and where to start a test. Relay both to whoever writes
  the test. Read them, do not pattern-match on them: the wording is prose, not an interface.
- An `Ignored` mutant reuses `statusReason` for the reason its annotation gave, if it gave one, and
  carries no `description`. No other status carries either field.
- Locations are 1-based. The `end` `column` is exclusive.
- Actionable survivors are the mutants with `"status": "Survived"`. Those are the gaps a test
  should close.
- Mutant order is deterministic (fixed generation order), so `id`s and the survivor list are
  stable across runs and safe to diff between attempts.
- `--html` is also machine-readable: the page embeds this same report in a
  `<script type="application/json" id="mutation-test-report">` block, so a report someone sent you as
  HTML can be parsed with no second file and no re-run. The embedded bytes differ from `--json`'s
  (packed onto one line, `</` escaped as `<\/` so it cannot end the `<script>` tag early, vs.
  `--json`'s pretty-printing), but parsed, the two are the same report.

## The survivor → killing-test loop

Same loop the [README's workflow section](../README.md#the-workflow) walks a human through
(pick a target, kill or annotate every survivor, re-run, move on), driven from JSON instead of the
console:

1. Run with `--json -`, capture stdout, and read `files[<path>].mutants`.
2. For each `"Survived"` mutant: it gives you a `location`, the `replacement` (the exact change
   no test caught), and the `description` / `statusReason` pair explaining the gap. Write or
   strengthen a test that *fails* under that change, usually an assertion pinned to the boundary or
   value the mutation moves.
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
(`if value > max_value`) can never be caught: at `value == max_value` the original falls through to
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
