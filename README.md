<h1 align="center">gdmutant</h1>
<p align="center"><strong>Mutation testing for GDScript and Godot — find the bugs your green tests would miss.</strong></p>

<p align="center">
  <a href="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg"></a>
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
test catches.

**Coverage tells you a line *ran*. Mutation tells you a bug there would be *caught*.** That gap is
gdmutant's whole job — and it widens when an AI writes the tests, since a model tends to pin the code
it just wrote. Mutation is one of the few executable, model-independent signals that a test actually
bites.

It's a standalone CLI (no AI required), validated
end-to-end against real Godot in CI. Reports use the `mutation-testing-elements` schema, so they
render in the existing HTML viewer for it.

## Is this for you?

- You write **GDScript** and test with **GUT**, **gdUnit4**, or any `godot --headless` command.
- You already have a test suite and want to know which of it actually bites.
- gdmutant is a natural companion to GUT and gdUnit4 alike — it doesn't replace your test runner, it
  grades it.

## Quickstart

Not on PyPI yet — install from git at a pinned commit (v0.1.0, in development; a PyPI release is
planned):

```sh
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
```

No Python in your game repo? Keep gdmutant in a tiny non-package uv project beside it (e.g. under
`devtools/`, with `[tool.uv] package = false`) so it never touches shipped code.

**See it work with no Godot at all** — point `--dry-run` at any `.gd` file to list the mutants
gdmutant would generate (from a clone of this repo, the bundled `corpus/` fixture prints the example
below):

```sh
uv run gdmutant run corpus/turn_order.gd --dry-run
```

**Run it for real on your project** — pick the runner for your framework.
**[GUT](https://github.com/bitwes/Gut)** and **[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4)**
are first-class peers: both read the framework's JUnit XML for per-test detail (gdUnit4 is the default
runner; select GUT with `--runner gut`):

```sh
uv run gdmutant run path/to/module.gd --project . --runner gut --json report.json   # GUT
uv run gdmutant run path/to/module.gd --project . --json report.json                # gdUnit4 (default)
```

**No JUnit XML?** Point the universal exit-code runner at the same headless test command your CI runs
— it only has to exit non-zero on failure (a hand-rolled `SceneTree` harness, a bespoke CLI):

```sh
uv run gdmutant run path/to/module.gd --project . \
  --runner command --command "godot --headless --script res://tests/run_tests.gd" --json report.json
```

## What it does

**Mutates the AST, not text.** Comparison, boolean, arithmetic, constant, numeric-literal,
compound-assignment, modulo, unary-not, and statement-deletion operators — each re-parse-guarded, so
invalid mutants never run. One file, many, or a whole directory in one pass, with a per-file
breakdown and one aggregate score.

**Runs your existing tests, framework-agnostically** — **gdUnit4** and **GUT** are first-class peer
runners (each reads its framework's JUnit XML for per-test detail, over one shared runner contract —
neither privileged), plus the universal **exit-code** runner (`--runner command`) for any headless
`godot` command that exits non-zero on failure, no addon required. Any future JUnit-emitting framework
is first-class by adding one small adapter. [How the runner seam works →](docs/decisions/0011-runner-agnostic-adapter-seam.md)

**Explains every survivor.** The console report doesn't just give a location — for each survivor it
shows the source line with a caret on the exact token, what's untested, why it matters, and where to
start a test. [Reading your first report →](docs/reading-your-first-report.md)

**Interoperates.** `--json` emits the
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema
and `--html` a viewer page with that data inlined; the viewer script loads from a pinned CDN, so the
page needs network to render. The JSON can post a score badge to a dashboard that reads it.

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

## Compatibility

You need **Python 3.12+** (managed with [uv](https://docs.astral.sh/uv/) — a dev tool, never shipped
with your game), **Godot** for real runs (`--dry-run` needs none), and a **test runner** — GUT or
gdUnit4 (each via its dedicated JUnit-XML runner + addon), or any `godot --headless` command via the
exit-code runner (no addon).

gdmutant only parses GDScript and shells out to your runner's headless CLI — both stable across Godot's
4.x minors — so it is version-tolerant by design.

| | CI-verified every push | Expected to work (best-effort) |
|---|---|---|
| **Godot** | 4.7.x | 4.3+ |
| **Runner** | GUT 9.7.1 + gdUnit4 6.1.3, each against real Godot | GUT 9.x, gdUnit4 6.x, any headless command |

Only the left column is tested each push. **GUT and gdUnit4 are first-class CI peers** — a dedicated
`selftest-gut` and `selftest-godot` job each install the pinned addon and drive the shipped CLI against
real Godot on the same corpus, to the same per-mutant outcome, so neither runner is second-tier. The
floor on the right is a claim, not a guarantee: gdmutant *should* run on Godot 4.3+ and current
GUT/gdUnit4 because of how little it touches — but if it breaks on a version there, please report it.

**Which runner.** GUT and gdUnit4 are peers; the exit-code runner covers everything else:

| Runner | Flag | Detail | Needs |
|---|---|---|---|
| **GUT** | `--runner gut` | per-test (JUnit XML) | the GUT addon |
| **gdUnit4** | `--runner gdunit4` (default) | per-test (JUnit XML) | the gdUnit4 addon |
| **exit-code command** | `--runner command --command "…"` | suite pass/fail | nothing (any headless `godot` command) |

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
