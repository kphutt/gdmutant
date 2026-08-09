---
type: how-to
status: active
created: 2026-07-11
---

# The gdmutant guide

A one-read guide to running gdmutant and acting on the results correctly: invoking the CLI, reading
its output, fixing what goes wrong, and wiring it into GitHub Actions. Written for a human and an
AI agent alike, since both need the exact same facts. This is a *how-to-use-the-tool* guide for
consumers, distinct from [`AGENTS.md`](../AGENTS.md), which tells a contributor how to work *on*
gdmutant's own source, not how to run it.

**Contents**

- [CLI](#cli)
  - [Install](#install)
  - [Invoke](#invoke)
    - [Runner selection](#runner-selection)
    - [Scoping a run](#scoping-a-run)
    - [Parallelism](#parallelism)
    - [Refusing a dirty tree](#refusing-a-dirty-tree)
    - [Job summary](#job-summary)
    - [Progress output](#progress-output)
  - [Keeping stdout parseable](#keeping-stdout-parseable)
  - [Project config (`.gdmutant.toml`)](#project-config-gdmutanttoml)
  - [Exit codes](#exit-codes-the-contract)
  - [How gdmutant writes to your files](#how-gdmutant-writes-to-your-files)
  - [Output schema](#output-schema-stryker-mutation-testing-elements-v2)
  - [The survivor → killing-test loop](#the-survivor--killing-test-loop)
  - [Worked example](#worked-example-the-bundled-corpus)
- [Troubleshooting](#troubleshooting)
  - [GitHub Actions-specific failures](#github-actions-specific-failures)
- [GitHub Actions](#github-actions)
  - [Inputs](#inputs)
  - [Outputs](#outputs)
  - [Pinning](#pinning)

## CLI

### Install

gdmutant is a Python CLI (Python 3.12+):

```sh
pip install 'gdmutant==0.1.*'   # gdmutant is 0.x: pin the minor so a new one is a move you make on purpose
```

Want a global command instead of a project dependency? [`pipx install
'gdmutant==0.1.*'`](https://pipx.pypa.io/) or [`uv tool install
'gdmutant==0.1.*'`](https://docs.astral.sh/uv/guides/tools/) work the same way, each in its own
isolated environment, and the same pin applies.

The README's [Quickstart](../README.md#quickstart) has the full setup.

### Invoke

```sh
gdmutant run <file.gd> --project <godot-project-dir> --runner gdunit4 --json -
```

| Flag | Default | What it does |
|---|---|---|
| `--project <dir>` | the source's own directory | The Godot project directory. |
| `--runner {gdunit4,gut,command}` | *(required, no default)* | Which test harness runs the suite. See [Runner selection](#runner-selection) below. |
| `--command <cmd>` | *(none)* | The test command, for `--runner command`. |
| `--godot <path>` | `godot` | The Godot executable. |
| `--tests <res://...>` | `res://test` | The test directory (gdUnit4's `-a` / GUT's `-gdir`). |
| `--json [-\|path]` | off | Write the Stryker JSON report. `-` streams to stdout, and a bare `--json` defaults to a timestamped filename. |
| `--html [path]` | off | Write a self-contained HTML report. A bare `--html` defaults to a timestamped filename. |
| `--report-path <rel>` | per-runner default | Where the JUnit XML report lands, relative to the project. |
| `--report step-summary` | off | Also write survivors to the GitHub Actions job summary (or stdout, if that variable is unset). |
| `--since <ref>` | mutate everything | Only mutate lines changed since a git ref: the per-PR mode. |
| `--exclude <glob>` | *(none)* | Skip matching files when expanding a directory (repeatable). |
| `--jobs N` / `-j N` | `1` | Run N mutants in parallel, each in its own project copy. |
| `--timeout <seconds>` | 10x the baseline run | Per-mutant test timeout. |
| `--require-clean` / `--no-require-clean` | warn only | Refuse to run on an uncommitted source file. |
| `--trust-config` | off | Act on `.gdmutant.toml`'s `command`/`godot`/`project` keys. |
| `--progress {auto,plain,none}` | `auto` | How much the run narrates itself while it works. |
| `--dry-run` | off | List the mutants without running any tests. |

The table above is the full flag set (`gdmutant run --help` shows the same list). The rest of this
section is the nuance behind the flags that have edge cases, grouped by topic.

#### Runner selection

`--runner` is required. There is no default. `gdunit4` and `gut` are first-class peer JUnit
adapters (per-test detail). No JUnit XML? `--runner command --command "<test cmd>"` takes any
command that exits non-zero on failure (e.g. a hand-rolled
`godot --headless --script res://tests/run_tests.gd`). Under `--runner command` the executable
comes from `--command` itself. `--godot` is not read in that mode, so put the full path inside
`--command`. See [`docs/decisions/0011`](decisions/0011-runner-agnostic-adapter-seam.md) (the
runner seam) and [`docs/decisions/0005`](decisions/0005-exit-code-test-runner-convention.md) (the
exit-code fallback).

#### Scoping a run

- `--since <ref>` mutates only the lines changed since a git ref (e.g. `--since origin/main`), the
  fast per-PR mode for CI. When nothing in the given paths changed since that ref, gdmutant runs no
  tests, exits 0, and still writes the report and job summary you asked for (an empty `mutants` list,
  no score), so `--json -` never needs a special case for it. A PR that touches no `.gd` file hits
  this every time.
- `--dry-run` lists the mutants gdmutant *would* generate, without Godot and without running any
  tests: a fast, dependency-free preview, useful before wiring up `--exclude` or scoping a large
  file. Not a substitute for a real run: it can't tell you anything your tests would actually catch.
  `gdmutant example && gdmutant run gdmutant-hello-world.gd --dry-run` shows the shape on the bundled
  demo file.

#### Parallelism

`--jobs N` runs N mutants at once, each inside its own copy of the project. Your own file is never
touched (see [How gdmutant writes to your files](#how-gdmutant-writes-to-your-files)). One
restriction: every file to mutate must sit inside `--project`, or the run exits 2.

#### Refusing a dirty tree

`--require-clean` refuses to run unless git holds a copy of the source it could restore (exit 2 on
uncommitted changes, a gitignored file, a file outside any repository, or a machine with no git).
Without it, gdmutant only *warns* and proceeds: it never blocks on a prompt, so it's safe for a
headless agent. `--no-require-clean` overrides a `.gdmutant.toml` that sets `require-clean = true`,
for the one run that has to go ahead on a dirty tree anyway.

#### Job summary

`--report step-summary` writes survivors, with their explanations, as Markdown to the GitHub Actions job summary
(`$GITHUB_STEP_SUMMARY`), or to stdout when that variable is unset: see [Keeping stdout
parseable](#keeping-stdout-parseable) for how that interacts with `--json -`. `step-summary` is the
only value it takes, and repeating the flag changes nothing. It's advisory: a failed write only warns,
and never changes the score or exit code. [GitHub Actions](#github-actions) covers the common case,
where the Action sets `$GITHUB_STEP_SUMMARY` itself.

#### Progress output

Nothing predicts a finish time: before the run gdmutant states the mutant count, during it a
heartbeat reports what's finished, and the closing `Done in ...` line gives the real wall-clock:
parse that, not the opening line. `--progress plain` forces the heartbeat cadence otherwise chosen
automatically off a TTY or in CI. `--progress none` silences the whole stream, including that
closing line, so use it only when you don't need the duration.

### Keeping stdout parseable

With `--json -` the report is the only thing on stdout. The human summary and per-mutant progress
go to stderr. No flag can quietly append to it:

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

### Project config (`.gdmutant.toml`)

gdmutant reads `.gdmutant.toml` from the directory it runs in. Its keys seed the defaults of the
flags of the same name (an explicit flag on the command line still wins):

```toml
# project needs --trust-config too, see below
project = "."
runner = "command"
# command and godot need --trust-config, see below
command = "godot --headless --script res://tests/run_tests.gd"
# godot = "/Applications/Godot.app/Contents/MacOS/Godot"
# exclude = ["*_generated.gd", "*/vendor/*"]
```

Three of those keys are trust-required: `command` and `godot` name a program gdmutant would
execute, and `project` names the directory every other operation is rooted in, the `cwd` of every
subprocess and, under `--jobs N`, a tree copied once per worker. In a checkout you did not write,
that file is somebody else's instruction about what runs and what gets read on your machine, so
gdmutant refuses to act on those three and exits 2 instead, even if you also pass the matching
flag yourself, since the file setting one of them at all is what needs your say-so, not just a
disagreement between the file and the flag. The refusal fires on any run, `--dry-run` included, and
it is the exit-2 cause most likely to surprise an agent working in a directory it did not create.

```sh
gdmutant run scripts --trust-config
```

`--trust-config` acts on the file's `command`, `godot`, and `project`. Use it only where the
checkout is trusted. Otherwise, remove those keys from `.gdmutant.toml` and pass them as flags
instead. Every other key is read normally: none of them can decide what gets executed or read, so
none of them ever need trust.

### Exit codes (the contract)

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
  - `.gdmutant.toml` sets `command`, `godot`, or `project` and `--trust-config` was not passed, or
    that file cannot be read or holds a value of the wrong type
  - the test-runner executable was not found: `godot`, or the program named by `--command`
  - `--runner command` with no `--command`, or a `--command` string that cannot be split into words
  - `--since` names a ref git cannot diff against
  - `--jobs` above 1 with a source file that does not sit inside `--project`
  - `--jobs` below 1
  - `--json -` and `--report step-summary` together with `$GITHUB_STEP_SUMMARY` unset: two
    documents, one stdout
  - a report file could not be written, or the source file could not be rewritten or put back

### How gdmutant writes to your files

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

### Output schema (Stryker `mutation-testing-elements`, v2)

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

### The survivor → killing-test loop

Same loop the [README's survivors section](../README.md#killing-survivors) walks a human through
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

### Worked example (the bundled corpus)

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

#### A killable survivor

The numeric mutant `0 -> -1` on `if value < 0` survives: at `value == -3`
both the original and the mutant return `0`, so the existing tests can't tell them apart. At
`value == -1` they differ: the original clamps to `0`, the mutant returns `-1`. The boundary test
kills it:

```gdscript
func test_clamp_initiative_lower_boundary() -> void:
	assert_int(TurnOrder.clamp_initiative(-1, 10)).is_equal(0)  # kills `< 0` -> `< -1`
```

#### A genuine equivalent

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

## Troubleshooting

- `gdmutant run: error: argument --html: expected one argument` (or a `--runner` choice list
  missing `gut`, or any other flag behaving like an old version). `pip install .`/`pip install
  gdmutant` warns when its `gdmutant.exe` lands somewhere not on PATH ("The script gdmutant.exe is
  installed in ... which is not on PATH"). If an older `gdmutant` from a different Python install
  is earlier on PATH, that's the one your shell runs, silently. Check `gdmutant --version` first.
  If it names an older release than the one you meant to install, that is the mismatch. Same if a
  tool run from a checkout silently prefers a stale git-pinned dependency over the one you just
  installed (`tool.uv.sources` in `pyproject.toml`, if the project has one). If the version looks
  right, check the path instead: `Get-Command gdmutant` (PowerShell) or `which gdmutant` (bash/zsh)
  shows which `gdmutant.exe` actually answers, and compare it against the path pip's warning named.
  Fix by adding that directory to PATH, or by uninstalling the older `gdmutant` first.
- "GUT found no tests …" on the baseline run. Nothing is broken: `--tests` defaults to
  `res://test`, GUT's layout puts suites in `test/unit/`, and `-gdir` doesn't search
  subdirectories. Pass `--tests res://test/unit`. For a *tree* of suites, run GUT yourself with
  `-ginclude_subdirs` behind `--runner command`.
- "GUT ran 0 tests" partway through a run. That one is real: a mutant broke a test file badly
  enough that GUT skipped its suite and ran the rest green. Reported as an error, not a survivor.
- The addon isn't found. The actual message names the missing directory and the fix directly:
  `error: the GdUnit4 addon was not found in the project: addons/gdUnit4/ is missing under
  <project>. Install GdUnit4 (Godot Asset Library), or run without the addon via --runner command
  --command "<your headless test command>".` GUT's version reads the same way with `addons/gut/`.
  GUT or gdUnit4 must already be installed and enabled in the project `--project` names. gdmutant
  installs neither.
- Everything survives. Usually the tests never ran: run your suite by hand first. Godot exits `0`
  even on a harness that fails to *compile*, so gate a `--command` harness on `can_instantiate()`.
- It's slow. Godot boots once per mutant. Mutate one file at a time, and use `--jobs N`, real but
  sub-linear (~3× at `--jobs 4`), since the workers contend for CPU and RAM. Watch the closing line
  for how much of the time was timeouts. A few hanging mutants can outweigh every other mutant
  combined, and `--timeout` caps each one.
- The first run goes quiet for minutes right after it starts. gdmutant does warn you first (the
  GUT/gdUnit4 runners print a "preparing the project" notice, and `--runner command` warns too when
  the project has no `.godot/` directory), but the wait itself is silent: that's Godot importing
  every asset in the project, once, which gdmutant can't see into or report on. Run `godot
  --headless --path <project> --import` yourself first so every later run starts in seconds.
- No JUnit XML? Point the exit-code runner at any headless command that exits non-zero on failure.
  `--godot <path>` only applies to the GUT/gdUnit4 runners above. Under `--runner command`, gdmutant
  runs your `--command` string exactly as given and never edits it, so if `godot` isn't on your
  PATH, write its full path directly into the command yourself:

  ```sh
  gdmutant run ../my-project/src/module.gd --project ../my-project --runner command --command "godot --headless --script res://tests/run_tests.gd" --json
  ```

### GitHub Actions-specific failures

- The "Run gdmutant" step logs the fully-resolved CLI invocation inside a collapsible
  `gdmutant command` group before running it, expand it first: it shows exactly what
  `--project`/`--runner`/`--tests` and everything else resolved to, which is usually enough to spot
  a wrong input on its own.
- `addon-version: <anything other than installed>` fails the step immediately with
  `::error::addon-version='<value>' is not supported yet — only 'installed'`. Cloning the addon at a
  ref instead of vendoring it is a planned fast-follow, not built yet.
- `since` set but the base commit isn't in your clone fails with `error: git diff for --since <ref>
  failed: <detail>`, or `error: could not run git for --since <ref>: <detail>` if git itself can't
  run. Add
  `fetch-depth: 0` to the workflow's `actions/checkout` step, its default fetches one commit, which
  usually doesn't include the base commit `since` needs.
- An invalid `godot-version` fails inside the underlying
  [`chickensoft-games/setup-godot`](https://github.com/chickensoft-games/setup-godot) step, before
  gdmutant itself ever runs, with whatever message that action produces. Check the version string
  against [Godot's release list](https://godotengine.org/download/archive/) first.

## GitHub Actions

gdmutant ships as a GitHub Action too (`kphutt/gdmutant`, wrapping the CLI). These examples are
bare steps to add into a workflow you already have. Starting from nothing instead? [The README's
GitHub Actions section](../README.md#github-actions) has a complete, standalone workflow file to
save as-is (see GitHub's [Quickstart for GitHub
Actions](https://docs.github.com/en/actions/quickstart) too, if you haven't written one before):

```yaml
- uses: kphutt/gdmutant@05728864a1c9330d632e2aab2348ff4442f3d61d # v0.1.0
  with:
    godot-version: "4.7.0"   # the only required input
    project-path: ./
```

This is the gdUnit4 default (`runner: gdunit4`): no `tests:` needed, since gdUnit4's usual layout
already matches gdmutant's own default, `res://test`. For GUT, whose stock layout puts suites in
`test/unit/` instead, set both `runner` and `tests` explicitly:

```yaml
- uses: kphutt/gdmutant@05728864a1c9330d632e2aab2348ff4442f3d61d # v0.1.0
  with:
    godot-version: "4.7.0"
    project-path: ./
    runner: gut
    tests: res://test/unit
```

It sets up Python and Godot, installs gdmutant, runs it, and writes every survivor (with its `gap`
/ `risk` / `start` explanation) to the workflow's job summary, where reviewers already look
(`job-summary: false` skips that). The job summary reads like this (a real run against this repo's
own bundled corpus, trimmed to one survivor):

````markdown
## gdmutant: mutation report

**Mutation score: 61.1%**

11 killed · 0 timeout · **7 survived** · 0 ignored · 0 invalid · 0 error

### Surviving mutants (7)

Each is a line a bug could live on that no test catches. gdmutant explains the gap, not just the location:

#### `corpus/turn_order.gd:13` · comparison

```gdscript
    if value < 0:
```

Changed `<` to `<=`: every test still passed.

**The gap.** Your tests pass whether this says `<` or `<=`. They run this line, but never with an
input the two decide differently, so what this comparison decides is untested.

**Why it matters, and where to start.**

Passing here is false confidence, not proof. A later refactor or merge that changes this
comparison slips through green. If it has a right answer, no test guards it.

Add a test that reaches this line with two equal operands (a value compared to itself) and assert
the result you expect. Equal operands separate every comparison swap gdmutant makes. Only you know
the result, gdmutant reports the gap, not it.

[Explain the `comparison` operator](https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md#comparison)
````

Every other survivor repeats that same shape (`path:line`, the changed source line, the gap, and
where to start). The `report-json` output holds the path to the `mutation-testing-elements`
report, ready to hand to an upload-artifact step. Survivors are output, not failure: the step
exits non-zero only on a real error, such as a red baseline suite. The project and a suite that
already passes must be there, plus the GUT or gdUnit4 addon if you use either. The action installs
none of that.

### Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `godot-version` | Yes | *(none)* | The Godot version to set up, e.g. `4.7.0`. |
| `project-path` | No | `./` | The Godot project directory (`--project`). |
| `paths` | No | the whole project | Source file(s)/directories to mutate, space-separated (gdmutant's positional targets). Excludes `addons/` and test files. |
| `runner` | No | `gdunit4` | Test runner: `gdunit4`, `gut`, or `command` (`--runner`). `command` has no dedicated input of its own, see below. |
| `tests` | No | gdmutant's default, `res://test` | The test directory (`--tests`). |
| `since` | No | mutate the full target | Only mutate lines changed since this git ref (`--since`): the fast, per-PR diff-scoped mode. |
| `args` | No | *(none)* | Extra raw arguments appended to the invocation, verbatim. Also how you pass `--command` (see below). |
| `job-summary` | No | `true` | Write survivors (with explanations) to the job summary as Markdown (`--report step-summary`). Set `false` to skip. |
| `godot-use-dotnet` | No | `false` | Set up the .NET (Mono) build of Godot instead of the standard build. |
| `addon-version` | No | `installed` | How the test-runner addon is provided. Only `installed` (already vendored in your project) ships today. Cloning the addon at a ref is a planned fast-follow. |
| `ref` | No | *(none)* | Install gdmutant from this git ref (tag/branch/SHA) instead of PyPI: the escape hatch for testing an unreleased commit. Most consumers never need this. |
| `gdmutant-version` | No | *(none)* | Install this exact published PyPI version instead of deriving one from the pinned `uses:` ref. Rarely needed. |

Setting both `ref` and `gdmutant-version` is unusual, but if you do, `gdmutant-version` wins and
installs from PyPI, `ref` is ignored entirely for that run. You are very unlikely to hit this: pick
one or the other.

`runner: command` has no dedicated input, since a headless test command varies too much to model
as one. Pass it through `args` instead, the same way the CLI's `--command` flag works:

```yaml
- uses: kphutt/gdmutant@05728864a1c9330d632e2aab2348ff4442f3d61d # v0.1.0
  with:
    godot-version: "4.7.0"
    project-path: ./
    runner: command
    args: --command "godot --headless --script res://tests/run_tests.gd"
```

`since` reads the base commit out of your clone, so the workflow's `actions/checkout` step needs
`fetch-depth: 0`. Its default fetches one commit, the base commit is not among them, and the
gdmutant step then fails on a git error rather than mutating anything.

If your project's `.gdmutant.toml` sets `command`, `godot`, or `project`, add `args:
--trust-config`. Without
it gdmutant refuses to run at all and the step fails, for the reason given under [Project
config](#project-config-gdmutanttoml) above.

### Outputs

| Output | Description |
|---|---|
| `report-json` | Path to the Stryker-format JSON mutation report the run wrote. |

### Pinning

Pin the commit SHA shown above, or a full `vX.Y.Z` tag (e.g. `@v0.1.0`). Never `@v1` or `@v0`: a
tag ruleset blocks deleting or re-pointing any tag for anyone acting normally, and the release
guard rejects a tag that doesn't equal the packaged version, so a floating major tag is not
something this repo can produce through ordinary use.
Pinning `@v0.1.0` works and is just as stable, for the same reason.

The bumps a floating tag would have handed you come from Dependabot instead, as PRs you review
before taking:

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
```
