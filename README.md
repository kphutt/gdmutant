<h1 align="center">
  <img src=".github/assets/banner.svg" alt="gdmutant: banner with Frank the Mutant, the project mascot" width="1200" height="320">
</h1>

<p align="center"><strong>Mutation testing for GDScript and Godot: find the bugs your green tests would miss.</strong></p>

<p align="center">
  <a href="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/gdmutant/"><img alt="PyPI" src="https://img.shields.io/pypi/v/gdmutant"></a>
  <a href="#compatibility"><img alt="Godot 4.3+" src="https://img.shields.io/badge/Godot-4.3%2B-478cbf?logo=godot-engine&logoColor=white"></a>
  <a href="https://github.com/bitwes/Gut"><img alt="GUT 9.x" src="https://img.shields.io/badge/GUT-9.x-478cbf"></a>
  <a href="https://github.com/godot-gdunit-labs/gdUnit4"><img alt="gdUnit4 6.x" src="https://img.shields.io/badge/gdUnit4-6.x-478cbf"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white"></a>
  <a href="https://github.com/kphutt/gdmutant/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center"><sub>A community tool, not affiliated with or endorsed by the Godot Foundation.</sub></p>

## What it is

gdmutant mutates your GDScript (flips `>`↔`>=`, `and`↔`or`, bumps a number, deletes a statement),
reruns your tests once per change, and reports the survivors: lines a bug could live on that no
test catches. Here's a real one:

```
Mutation score: 61.1%

──── survived ─────────────────────────────────────────────── boolean ────

  corpus\turn_order.gd:27   func can_act

     27 |     return alive and not stunned
        |                  ^  changed  and  to  or — every test still passed

  gap    Your tests pass whether this needs both sides (`and`) or just one
         (`or`). No test covers the case that tells them apart: the
         operands disagreeing (one true, one false).

  risk   Your tests can't tell 'needs both' from 'needs either.' A change
         that loosens or tightens this guard would pass every test.

  start  Add a test where exactly one side is true and the other false,
         and assert the outcome.

  more   https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md#boolean
──────────────────────────────────────────────────────────────────────────
```

Coverage tells you a line *ran*. Mutation tells you a bug there would be *caught*. Those are
not the same question, and the more code a project ships (whoever or whatever wrote it), the
more of it rides on the answer. A standalone CLI, no AI required.

## Is this for you?

- You write GDScript and test with GUT, gdUnit4, or any `godot --headless` command.
- You already have a Godot project whose tests pass, however you run them (with GUT or gdUnit4 that
  means its addon is installed and enabled). gdmutant grades those tests, it doesn't replace them.
  No project yet? Try `--dry-run` below first.
- Not GDScript? gdmutant reads GDScript and nothing else. Same idea, other languages:
  [mutmut](https://github.com/boxed/mutmut) for Python, [Stryker](https://stryker-mutator.io/) for
  JS/TS, [PIT](https://pitest.org/) for Java.

## Prerequisites

Godot 4.3+, a test suite that already passes, and the GUT or gdUnit4 addon if you use either.

A project Godot has already imported. On a checkout Godot has never opened, it imports every
asset before it will run anything: minutes of total silence on a real game, which is easy to read
as a hung tool. `--runner gdunit4` and `--runner gut` do that warm-up for you and say so.
`--runner command` cannot: it only knows the command you hand it. Do it once yourself, and every
later run starts in seconds:

```sh
godot --headless --path path/to/your-game --import
```

Python 3.12+, and if you don't have Python, don't go install it. Install
[uv](https://docs.astral.sh/uv/) instead: it fetches its own Python and runs gdmutant on it.

```sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Already have Python 3.12+? `pip install gdmutant` works just as well: drop the `uv run` prefix
from every command below.

## Quickstart

Make gdmutant a small uv project in a folder beside your game, never inside it: `uv init` drops
`.git`, `.gitignore`, `.python-version`, `main.py`, `pyproject.toml` and a `README.md` of its own
wherever you run it, and none of that belongs in a game repo. Pin the interpreter: `uv init` alone
floors on whatever it finds, and gdmutant needs 3.12+.

```sh
uv init --python 3.12 gdmutant-workspace
cd gdmutant-workspace
uv add gdmutant
```

Save this as `scratch.gd`:

```gdscript
static func clamp_initiative(value: int, max_value: int) -> int:
	if value < 0:
		return 0
	if value > max_value:
		return max_value
	return value
```

then preview its mutants. `--dry-run` lists what *would* be mutated and stops there (no test run,
no Godot):

```sh
uv run gdmutant run scratch.gd --dry-run
```

Point it at your own file instead once you have one. The preview is not the payoff. A real run
reruns your tests once per mutant and reports the survivors: lines where a bug could live that
no test catches. Finding those is what the tool is for.

For a real run, pick your runner. [GUT](https://github.com/bitwes/Gut) and
[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4) are peer JUnit-XML readers (gdUnit4 is the
default). Both need their addon already installed and Godot itself on PATH, or `--godot <path>`.
The exit-code runner further down needs neither. `--tests` names the one directory holding your
suites. It defaults to `res://test`, but GUT's stock layout is `test/unit/` and GUT's `-gdir` does
not search subdirectories:

```sh
# GUT
uv run gdmutant run ../my-game/src/module.gd --project ../my-game \
  --runner gut --tests res://test/unit --json report.json

# gdUnit4 (the default)
uv run gdmutant run ../my-game/src/module.gd --project ../my-game --json report.json
```

No JUnit XML? Point the exit-code runner at any headless command that exits non-zero on
failure. `--godot` doesn't reach inside this string. Put the Godot path in `--command` itself:

```sh
uv run gdmutant run ../my-game/src/module.gd --project ../my-game \
  --runner command --command "godot --headless --script res://tests/run_tests.gd" --json report.json
```

## What it does

- Mutates the AST (your code's parsed structure), not text. Nine re-parse-guarded operators
  ([full list](docs/survivors/README.md)), so every mutant that runs is valid GDScript. A
  text-level edit can produce code that does not compile, and a run that measures nothing. One
  file, many, or a whole directory in one pass, with a per-file breakdown and one aggregate score.
- Runs your existing tests: gdUnit4, GUT, or any headless `godot` exit-code command.
- Explains every survivor: not just a location, but what's untested, why it matters, and where
  to start a test.
- Interoperates and fits CI. `--json` emits the
  [`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements)
  schema for dashboards. `--html` writes one self-contained page (no network, no CDN) that marks
  every survivor on its own source line and explains the gap beside it. `--since <ref>` mutates
  only a PR's changed lines, for a fast, advisory check, never a hard gate. `--exclude` globs.
  `.gdmutant.toml` holds per-project defaults. There's a [GitHub Action](#github-action) too.

gdmutant scores every run. The counts behind the block above come from this repo's `corpus/`
fixture. The wheel you `pip install` leaves it out, so reproducing this takes a checkout or the
source distribution, which does carry it:

```
Mutation score: 61.1%
  killed:   11
  timeout:  0  (counted as killed)
  survived: 7
  ignored:  0  (suppressed, excluded from score)
  invalid:  0
  error:    0
```

Mutation score isn't a target, it's a direction. There's no universal "good" number. Watch
it trend as you kill survivors. See the [survivor reference](docs/survivors/README.md) for what
`ignored`, `invalid` and `error` mean.

## The workflow

1. Pick a target. Start with the file you trust least: new code, thin tests, or core logic
   (state machines, scoring, boundaries). One file at a time. A whole directory's survivor list is
   too much to work through at once.
2. Run it (see Quickstart above).
3. Kill or annotate every survivor. Each block's `start` line says where to add a test. A
   proven equivalent (one that truly can't change behavior) gets `# gdmutant: ignore` plus a
   reason on that line instead ([details](docs/survivors/README.md)).
4. Re-run to confirm, then move on: done with a file at zero `survived`. No third state,
   so the loop always ends.

gdmutant boots Godot once per mutant, so time scales with mutant count. It tells you
what the run is before it starts, keeps you posted while it goes, and prints the wall-clock when it
finishes. But it never predicts a finish time, because it cannot do that honestly (see
[Troubleshooting](#troubleshooting)). `--jobs N` runs N at once. The run below passed
`--timeout 30`. Left alone, gdmutant caps each mutant at ten times the baseline suite, with a
floor of 10s and a ceiling of 600s:

```
18 mutants to run. Baseline suite 1.4s; each mutant is capped at 30s.
… 7/18 done in 1m 12s — 2 survived, 1 timed out.
Done in 6m 32s — 18 mutants, 8 timed out (4m 0s of that). Baseline suite 1.4s.
```

The heartbeat lands every 30s on a terminal, and less often in a log or in CI. `--progress plain`
forces the quieter cadence. `--progress none` turns it off.

## Compatibility

| | Verified at every release | Expected to work |
|---|---|---|
| Godot | 4.7.0 | 4.3+ |
| Runner | GUT 9.7.1, gdUnit4 6.1.3 | GUT 9.x, gdUnit4 6.x, any headless command |

gdmutant is `0.x`, so anything can change between minor versions: flags, exit codes, console output.
Pin `gdmutant==0.1.*` in CI so a new minor is a version you move to on purpose, and take a `1.0` as
the point where the CLI surface and exit codes stop moving.

## Configuration

Persist per-project defaults in `.gdmutant.toml` (anything you pass on the command line wins):

```toml
project = "."
runner = "command"
# command and godot need --trust-config, see below
command = "godot --headless --script res://tests/run_tests.gd"
# godot = "/Applications/Godot.app/Contents/MacOS/Godot"
# exclude = ["*_generated.gd", "*/vendor/*"]
```

Two of those keys (`command` and `godot`) name a program gdmutant will run. gdmutant reads
`.gdmutant.toml` from the directory you are in, so in a project you cloned that file was written
by somebody else. It therefore refuses to act on those two keys unless you say the file is
trustworthy:

```
gdmutant run scripts --trust-config
```

Without `--trust-config` gdmutant names the keys the file decided, exits 2, and runs nothing. Not
even a `--dry-run` preview gets through, because the refusal comes first. It fires only where the
file actually decides the program: a key whose value matches gdmutant's own default, or one you
also passed as a flag, decides nothing, so the run goes ahead without a word. Passing the value as
a flag (`--command ...`, `--godot ...`) needs no trust, since then you named the program yourself.
Every other key (`project`, `runner`, `tests`, `report-path`, `timeout`, `require-clean`,
`exclude`) is read normally: none of them can decide what gets executed.

## GitHub Action

gdmutant ships as a GitHub Action, so a workflow can run it without installing Python, Godot or
gdmutant itself:

```yaml
- uses: kphutt/gdmutant@REPLACE_WITH_THE_RELEASE_COMMIT_SHA  # v0.1.0
  with:
    godot-version: "4.7.0"      # the only required input
    project-path: ./            # gdmutant's --project
    paths: scripts              # what to mutate (default: the whole project)
    runner: gdunit4             # gdunit4 | gut | command
    tests: res://test/unit      # the one directory holding your suites
    since: ${{ github.event.pull_request.base.sha }}   # mutate only this PR's changed lines
    args: --jobs 4              # any extra gdmutant flags, verbatim
```

`since` reads the base commit out of your clone, so the workflow's `actions/checkout` step needs
`fetch-depth: 0`. Its default fetches one commit, the base commit is not among them, and the
gdmutant step then fails on a git error rather than mutating anything.

It sets up Python and Godot, installs gdmutant, runs it, and writes every survivor (with its
`gap` / `risk` / `start` explanation) to the workflow's job summary, where reviewers already look
(`job-summary: false` skips that). The `report-json` output holds the path to the
`mutation-testing-elements` report, ready to hand to an upload-artifact step.
`godot-use-dotnet: true` picks the .NET build of Godot. Survivors are output, not failure: the step
exits non-zero only on a real error, such as a red baseline suite. The project and a suite that
already passes must be there, plus the GUT or gdUnit4 addon if you use either. The action installs
none of that.

If your project's `.gdmutant.toml` sets `command` or `godot`, add `args: --trust-config`. Without
it gdmutant refuses to run at all and the step fails, for the reason given under
[Configuration](#configuration).

Pin the commit SHA. There is no `@v1` or `@v0`. Every published tag names a full version
(`v0.1.0`) and never moves: a tag ruleset blocks deleting or re-pointing any tag, and the release
guard rejects a tag that doesn't equal the packaged version, so a floating major tag is not
something this repo can produce. Pinning `@v0.1.0` works and is just as stable, for the same
reason. Replace the placeholder above with the 40-character commit SHA the release was cut from,
and keep the version in the trailing comment so the line stays readable.

The bumps a floating tag would have handed you come from Dependabot instead, as PRs you can read
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

## Troubleshooting

- `pip` or `python` isn't recognized, or `python` opens the Microsoft Store. You have no Python.
  Install uv (see [Prerequisites](#prerequisites)). It brings its own.
- "GUT found no tests …" on the baseline run. Nothing is broken: `--tests` defaults to
  `res://test`, GUT's layout puts suites in `test/unit/`, and `-gdir` doesn't search
  subdirectories. Pass `--tests res://test/unit`. For a *tree* of suites, run GUT yourself with
  `-ginclude_subdirs` behind `--runner command`.
- "GUT ran 0 tests" partway through a run. That one is real: a mutant broke a test file badly
  enough that GUT skipped its suite and ran the rest green. Reported as an error, not a survivor.
- The addon isn't found. GUT or gdUnit4 must already be installed and enabled in the project
  `--project` names. gdmutant installs neither.
- `godot` isn't on PATH. Pass `--godot <full-path>`. It has no effect inside `--command`. Put
  the full path in that string yourself.
- Everything survives. Usually the tests never ran: run your suite by hand first. Godot exits `0`
  even on a harness that fails to *compile*, so gate a `--command` harness on `can_instantiate()`.
- It's slow. Godot boots once per mutant. Mutate one file at a time, and use `--jobs N`, real
  but sub-linear (~3× at `--jobs 4`), since the workers contend for CPU and RAM. Watch the closing
  line for how much of the time was timeouts. A few hanging mutants can outweigh every other
  mutant combined, and `--timeout` caps each one.
- How long will it take? gdmutant will not guess, and no other mutation tester does either. Up
  front it tells you what the run *is*: how many mutants, and the cap on each, so you know how
  long silence is normal. A rate estimate was built and measured before being dropped: on an even
  workload it tracked the true finish to within 5%, but on a run whose hanging mutants arrived
  late it read 3.2s at 25% done for a run that took 58s. A mutant that hangs costs its whole
  timeout, and nothing before it hints that it will.
- The first run sits there for minutes and never starts. The project has not been imported. See
  [Prerequisites](#prerequisites). Run `godot --headless --path <project> --import` once. Under
  `--runner command` gdmutant says so up front when the project has no `.godot/` directory.
- Most survivors are on `assert` lines. Expected, and not noise you have to fix: a failed `assert`
  kills the Godot process, so no in-process test can catch a weakened one. gdmutant explains each
  one and counts them under the survivor list rather than hiding them
  ([the full story](docs/survivors/README.md#assert)).

## How it works

A language-neutral loop (select → mutate → run → tally → score) with two language-specific
pieces behind an adapter: mutating the AST (via
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)) and running the tests.

Safety: in a serial run gdmutant edits your source file where it lies, then restores its exact
original bytes after every mutant and on exit, including Ctrl-C. Under `--jobs N` with N above 1
each worker mutates its own copy of the project, so your own file is never touched. Every write
goes to a temporary file beside the source and is then renamed over it, so the path always holds
one whole file or the other, never half of each.

Two things can still leave the mutant sitting on disk. A hard kill (a crash, a power loss) stops
the restore before it can run, and if it lands between the temporary write and the rename it also
leaves a stray `.<name>.<random>.tmp` beside your source, which is safe to delete. And a write
that cannot finish at all (a full disk, or an antivirus or editor lock that outlasts the retries)
ends the run with an error rather than retrying unsafely, which leaves the mutant where your
original was. That one needs no crash. Either way, put the file back from git.

Commit or stash first, or pass `--require-clean`, which refuses to start unless git already holds
a committed copy of every file it is about to mutate. That is stricter than a clean tree: a
project not in git, a file git ignores, and git not being installed all fail it too, because none
of them leave a copy to put back.

## Documentation

- [Survivor reference](docs/survivors/README.md): every operator explained, the score formula, how to kill or justify each.
- [Design & architecture](docs/design/DESIGN.md): the engine and the "Saboteur & the Jury" design.
- [Driving gdmutant from an AI agent](docs/using-with-an-ai-agent.md): invocation, JSON schema, the loop for a script.
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Credits](docs/credits.md)

## License

[MIT](LICENSE), © 2026 kphutt.
