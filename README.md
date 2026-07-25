<h1 align="center">gdmutant</h1>
<p align="center"><strong>Mutation testing for GDScript and Godot — find the bugs your green tests would miss.</strong></p>

<p align="center">
  <a href="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg"></a>
  <a href="#requirements"><img alt="Godot 4.4+" src="https://img.shields.io/badge/Godot-4.4%2B-478cbf?logo=godot-engine&logoColor=white"></a>
  <a href="https://github.com/godot-gdunit-labs/gdUnit4"><img alt="GdUnit4 6.0–6.1" src="https://img.shields.io/badge/GdUnit4-6.0%E2%80%936.1-478cbf"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white"></a>
  <a href="https://github.com/kphutt/gdmutant/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

## What it is

gdmutant mutates your GDScript — flips `>`↔`>=`, `and`↔`or`, bumps a number, deletes a statement —
reruns your tests once per change, and reports the **survivors**: lines a bug could live on that no
test catches.

**Coverage tells you a line *ran*. Mutation tells you a bug there would be *caught*.** That gap is
gdmutant's whole job — and it widens when an AI writes the tests, since a model tends to pin the code
it just wrote. Mutation is one of the few executable, model-independent signals that a test actually
bites.

It's a standalone CLI (no AI required), validated
end-to-end against real Godot in CI. Reports use Stryker's `mutation-testing-elements` schema, so
they render in tooling the JS/TS world already has.

## Is this for you?

- You write **GDScript** and test with **gdUnit4**, **GUT**, or any `godot --headless` command.
- You already have a test suite and want to know which of it actually bites.
- gdmutant is the natural companion to gdUnit4 — it doesn't replace your test runner, it grades it.

## Quickstart

Not on PyPI yet — install from git at a pinned commit (v0.1.0, in development; a PyPI release is
planned):

```sh
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
```

No Python in your game repo? Keep gdmutant in a tiny non-package uv project beside it (e.g. under
`devtools/`, with `[tool.uv] package = false`) so it never touches shipped code.

**See it work with no Godot at all** — point `--dry-run` at any `.gd` file to list the mutants
gdmutant would generate (from a clone of this repo, the bundled `corpus/` fixture prints the
example below):

```sh
uv run gdmutant run corpus/turn_order.gd --dry-run
```

**Run it for real on your project** — point it at the same headless test command your CI runs (it
only has to exit non-zero on failure; GUT, gdUnit4's CLI, and hand-rolled `SceneTree` harnesses all
qualify):

```sh
uv run gdmutant run path/to/module.gd --project . \
  --runner command --command "godot --headless --script res://tests/run_tests.gd" --json report.json
```

Already on **[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4)**? Drop `--runner command` — the
default runner reads gdUnit4's JUnit XML for per-test detail.

## What it does

**Mutates the AST, not text.** Comparison, boolean, arithmetic, constant, numeric-literal,
compound-assignment, modulo, unary-not, and statement-deletion operators — each re-parse-guarded, so
invalid mutants never run. One file, many, or a whole directory in one pass, with a per-file
breakdown and one aggregate score.

**Runs your existing tests, three ways** — a dedicated **gdUnit4** runner (reads JUnit XML for
per-test detail) plus the universal **exit-code** runner (`--runner command`) that drives **GUT**,
gdUnit4's CLI, or any headless `godot` command that exits non-zero on failure, no addon required.

**Explains every survivor.** The console report doesn't just give a location — for each survivor it
shows the source line with a caret on the exact token, what's untested, why it matters, and where to
start a test. [Reading your first report →](docs/reading-your-first-report.md)

**Interoperates.** `--json` emits Stryker's
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema
and `--html` a self-contained viewer page; both render in the Stryker ecosystem and can post a score
badge to the Stryker Dashboard.

**Fits real projects.** Test suites auto-skipped; `--exclude` globs; `.gdmutant.toml` for per-project
defaults; `--jobs N` evaluates mutants in parallel; `--since <ref>` mutates only the lines a PR
changed (the fast, per-PR mode); `--dry-run` lists mutants without booting Godot.

## Example output

```
18 mutants for corpus/turn_order.gd:
  corpus/turn_order.gd:8:17   comparison   > -> >=
  corpus/turn_order.gd:13:11  comparison   < -> <=
  corpus/turn_order.gd:27:15  boolean      and -> or
  corpus/turn_order.gd:32:9   constant     true -> false
  ...
```

A real run adds a killed/survived verdict per mutant, a mutation score, and the plain-language
survivor explanation described above.

## Requirements

- **Python 3.12+**, managed with [uv](https://docs.astral.sh/uv/) — a dev tool, not a runtime
  dependency; it never touches shipped game code.
- **Godot 4.4+** for real runs (`--dry-run` needs none).
- **gdUnit4** only for the gdUnit4 runner — the exit-code runner needs no addon.

**gdUnit4 compatibility:** v6.0–v6.1 (tested against **v6.1.3**), via gdUnit4's stable
`GdUnitCmdTool.gd` command-line contract, unchanged across that range. v6.1.x is the largest
in-the-wild bucket; v6.0.x is what the Godot Asset Library ships to new users — gdmutant targets both.

## Configuration

Persist per-project defaults in a `.gdmutant.toml` (any explicit flag overrides it):

```toml
project = "."
runner = "command"
command = "godot --headless --script res://tests/run_tests.gd"
# exclude = ["*_generated.gd", "*/vendor/*"]
```

Run `gdmutant run --help` for the full flag list.

## Continuous integration

gdmutant answers what a green build can't — *do the tests actually bite?* Run it **advisory**
(report-mode, never a hard gate), complementary to coverage. Add `--since origin/main` to mutate only
the lines a PR changed, turning an overnight batch into a per-PR check. The
[agent guide](docs/agent-guide.md) has the CI invocation, JSON schema, and exit-code contract.

## How it works

A language-neutral loop — select → mutate → run → tally killed/survived → score — with two
language-specific pieces behind an adapter: mutating the AST (via
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)) and running the tests. A new language is
one small adapter. Full design: [`docs/design/DESIGN.md`](docs/design/DESIGN.md).

**Safety:** gdmutant edits source **in place** and restores it after each mutant and on exit (or
Ctrl-C). Commit or stash before a run; `--require-clean` refuses to start with uncommitted changes.

## Documentation

- [Reading your first report](docs/reading-your-first-report.md) — verdicts, kill hints, equivalent mutants.
- [Design & architecture](docs/design/DESIGN.md) — the engine and the "Saboteur & the Jury" design.
- [Driving gdmutant from an AI agent](docs/agent-guide.md) — invocation, JSON schema, survivor→killing-test loop.
- [Exit-code runner convention](docs/decisions/0005-exit-code-test-runner-convention.md) — the stdout/exit-code contract.
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Credits](CREDITS.md)

## License

[MIT](LICENSE) — © 2026 Karsten Huttelmaier.
