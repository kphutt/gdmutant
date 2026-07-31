<h1 align="center">
  <img src=".github/assets/banner.svg" alt="gdmutant — banner with Frank the Mutant, the project mascot" width="1200" height="320">
</h1>

<p align="center"><strong>Mutation testing for GDScript and Godot — find the bugs your green tests would miss.</strong></p>

<p align="center">
  <a href="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/gdmutant/"><img alt="PyPI" src="https://img.shields.io/pypi/v/gdmutant"></a>
  <a href="#compatibility"><img alt="Godot 4.3+" src="https://img.shields.io/badge/Godot-4.3%2B-478cbf?logo=godot-engine&logoColor=white"></a>
  <a href="https://github.com/bitwes/Gut"><img alt="GUT 9.x" src="https://img.shields.io/badge/GUT-9.x-478cbf"></a>
  <a href="https://github.com/godot-gdunit-labs/gdUnit4"><img alt="gdUnit4 6.x" src="https://img.shields.io/badge/gdUnit4-6.x-478cbf"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white"></a>
  <a href="https://github.com/kphutt/gdmutant/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center"><sub>A community tool — not affiliated with or endorsed by the Godot Foundation.</sub></p>

## What it is

gdmutant mutates your GDScript — flips `>`↔`>=`, `and`↔`or`, bumps a number, deletes a statement —
reruns your tests once per change, and reports the **survivors**: lines a bug could live on that no
test catches. Here's a real one:

```
Mutation score: 61.1%

──── survived ──────────────────────────────────────────── comparison ────

  corpus\turn_order.gd:13   func clamp_initiative

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

Coverage tells you a line *ran*; mutation tells you a bug there would be *caught* — a gap that
widens when an AI writes the tests, since models tend to pin code they just wrote. A standalone
CLI, no AI required.

**Validated against real code.** gdmutant has been run against the
[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4) and [GUT](https://github.com/bitwes/Gut)
test frameworks, and surfaced defects in both — each confirmed by execution.

## Is this for you?

- You write **GDScript** and test with **GUT**, **gdUnit4**, or any `godot --headless` command.
- You already have a Godot project with the addon installed and a test suite passing — gdmutant
  grades those tests, it doesn't replace them. No project yet? Try `--dry-run` below first.
- **Not GDScript?** gdmutant reads GDScript and nothing else. Same idea, other languages:
  [mutmut](https://github.com/boxed/mutmut) for Python, [Stryker](https://stryker-mutator.io/) for
  JS/TS, [PIT](https://pitest.org/) for Java.

## Prerequisites

**Godot 4.3+**, the GUT or gdUnit4 addon installed, and a test suite that already passes.

**Python 3.12+** — and if you don't have Python, don't go install it. Install
[uv](https://docs.astral.sh/uv/) instead: it fetches its own Python and runs gdmutant on it.

```sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Already have Python 3.12+? `pip install gdmutant` works just as well — drop the `uv run` prefix
from every command below.

## Quickstart

Make gdmutant a small uv project **in a folder beside your game, never inside it** — `uv init` drops
`.git`, `main.py`, `pyproject.toml` and `.venv` wherever you run it, and none of that belongs in a
game repo. Pin the interpreter: `uv init` alone floors on whatever it finds, and gdmutant needs
3.12+.

```sh
uv init --python 3.12 gdmutant-workspace
cd gdmutant-workspace
uv add gdmutant
```

**No Godot needed yet.** Save this as `scratch.gd`:

```gdscript
static func clamp_initiative(value: int, max_value: int) -> int:
	if value < 0:
		return 0
	if value > max_value:
		return max_value
	return value
```

then list its mutants — no test run, no Godot:

```sh
uv run gdmutant run scratch.gd --dry-run
```

Point it at your own file instead once you have one.

**For real, pick your runner.** Needs the addon already installed and Godot itself on PATH, or
`--godot <path>`. [GUT](https://github.com/bitwes/Gut) and
[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4) are peer JUnit-XML readers (gdUnit4 is the
default). `--tests` names the one directory holding your suites — it defaults to `res://test`, but
GUT's stock layout is `test/unit/` and GUT's `-gdir` does not search subdirectories:

```sh
# GUT
uv run gdmutant run ../my-game/src/module.gd --project ../my-game \
  --runner gut --tests res://test/unit --json report.json

# gdUnit4 (the default)
uv run gdmutant run ../my-game/src/module.gd --project ../my-game --json report.json
```

**No JUnit XML?** Point the exit-code runner at any headless command that exits non-zero on
failure. `--godot` doesn't reach inside this string — put the Godot path in `--command` itself:

```sh
uv run gdmutant run ../my-game/src/module.gd --project ../my-game \
  --runner command --command "godot --headless --script res://tests/run_tests.gd" --json report.json
```

## What it does

**Mutates the AST, not text** — nine re-parse-guarded operators
([full list](docs/survivors/README.md)), so invalid mutants never run. One file, many, or a whole
directory in one pass, with a per-file breakdown and one aggregate score.

**Runs your existing tests** — gdUnit4, GUT, or any headless `godot` exit-code command.

**Explains every survivor** — not just a location, but what's untested, why it matters, and where
to start a test.

**Interoperates and fits CI.** `--json` emits the
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema
for dashboards; `--html` renders a self-contained viewer. `--since <ref>` mutates only a PR's
changed lines, for a fast, **advisory** check — never a hard gate. `--exclude` globs;
`.gdmutant.toml` holds per-project defaults.

**Scores every run.** The counts behind the block above — from this repo's `corpus/` fixture, so
reproducing it takes a checkout; the package doesn't ship it:

```
Mutation score: 61.1%
  killed:   11
  timeout:  0  (counted as killed)
  survived: 7
  ignored:  0  (suppressed, excluded from score)
  invalid:  0
  error:    0
```

**Mutation score isn't a target — it's a direction.** There's no universal "good" number; watch it
trend as you kill survivors. See the [survivor reference](docs/survivors/README.md) for what
`ignored`, `invalid` and `error` mean.

## The workflow

1. **Pick a target.** Start with the file you trust least — new code, thin tests, or core logic
   (state machines, scoring, boundaries). One file at a time; a whole directory's survivor list is
   too much to work through at once.
2. **Run it** (see Quickstart above).
3. **Kill or annotate every survivor.** Each block's `start` line says where to add a test. A
   proven equivalent — one that truly can't change behavior — gets `# gdmutant: ignore` plus a
   reason on that line instead ([details](docs/survivors/README.md)).
4. **Re-run to confirm**, then **move on** — done with a file at zero `survived`. No third state,
   so the loop always ends.

**Runtime.** gdmutant boots Godot once per mutant, so time scales with mutant count — it says so up
front, and `--jobs N` runs N at once:

```
18 mutants; baseline ~1.4s each → estimated ≈ 24s
```

## Compatibility

| | Verified at every release | Expected to work |
|---|---|---|
| **Godot** | 4.7.x | 4.3+ |
| **Runner** | GUT 9.7.1, gdUnit4 6.1.3 | GUT 9.x, gdUnit4 6.x, any headless command |

## Configuration

Persist per-project defaults in `.gdmutant.toml` (any explicit flag overrides it):

```toml
project = "."
runner = "command"
command = "godot --headless --script res://tests/run_tests.gd"
# exclude = ["*_generated.gd", "*/vendor/*"]
```

## Troubleshooting

**`pip` or `python` isn't recognized, or `python` opens the Microsoft Store.** You have no Python.
Install uv (see [Prerequisites](#prerequisites)) — it brings its own.

**"GUT found no tests …" on the baseline run.** Nothing is broken: `--tests` defaults to
`res://test`, GUT's layout puts suites in `test/unit/`, and `-gdir` doesn't search subdirectories.
Pass `--tests res://test/unit`. For a *tree* of suites, run GUT yourself with `-ginclude_subdirs`
behind `--runner command`.

**"GUT ran 0 tests" partway through a run.** That one is real: a mutant broke a test file badly
enough that GUT skipped its suite and ran the rest green. Reported as an error, not a survivor.

**The addon isn't found.** GUT or gdUnit4 must already be installed and enabled in the project
`--project` names — gdmutant installs neither.

**`godot` isn't on PATH.** Pass `--godot <full-path>`. It has no effect inside `--command`; put the
full path in that string yourself.

**Everything survives.** Usually the tests never ran — run your suite by hand first. Godot exits
`0` even on a harness that fails to *compile*, so gate a `--command` harness on `can_instantiate()`.

**It's slow.** Godot boots once per mutant. Mutate one file at a time, and use `--jobs N` — real
but sub-linear (~3× at `--jobs 4`), since every worker copies the project.

## How it works

A language-neutral loop — select → mutate → run → tally → score — with two language-specific
pieces behind an adapter: mutating the AST (via
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)) and running the tests.

**Safety:** gdmutant edits your source file **in place**, then restores its exact original bytes
after every mutant and on exit, including Ctrl-C. Only a hard kill (a crash, a power loss) can leave
a swap in place — commit or stash first, or pass `--require-clean` to refuse a dirty tree.

## Documentation

- [Survivor reference](docs/survivors/README.md) — every operator explained, the score formula, how to kill or justify each.
- [Design & architecture](docs/design/DESIGN.md) — the engine and the "Saboteur & the Jury" design.
- [Driving gdmutant from an AI agent](docs/using-with-an-ai-agent.md) — invocation, JSON schema, the loop for a script.
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Credits](docs/credits.md)

## License

[MIT](LICENSE) — © 2026 kphutt.
