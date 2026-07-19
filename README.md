<h1 align="center">gdmutant</h1>
<h3 align="center">Mutation testing for GDScript and Godot</h3>

<p align="center">
  <a href="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Godot 4.4+" src="https://img.shields.io/badge/Godot-4.4%2B-478cbf?logo=godot-engine&logoColor=white">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

<p align="center"><em>gdmutant is a provisional codename, not yet cleared for public use — it lives only in this README and the repo name.</em></p>

## What is gdmutant?

gdmutant is a mutation-testing tool for GDScript. Point it at a module with a test suite and it
mutates the source — flip `>` ↔ `>=`, `and` ↔ `or`, bump a number, delete a statement — reruns the
tests once per mutant, and reports the **survivors**: lines a bug could live on that no test would
catch.

Coverage says a line *ran*; mutation says a bug there would be *caught*. That gap is what gdmutant
finds — and it is exactly the gap that widens when an AI writes the tests, since a model tends to pin
the code it just wrote. Mutation is one of the few executable, model-independent signals that a test
actually bites.

It is a standalone CLI — no AI required — built on a language-neutral engine with a
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit) AST adapter, and validated end-to-end
against real Godot in CI (both runners, pinned to exact per-mutant outcomes).

## Features

**Mutation**
- AST-based operators: comparison, boolean, arithmetic, constant, numeric-literal, compound
  assignment, modulo, unary-not, and statement-deletion — each re-parse-guarded so invalid mutants
  are never run.
- One file, several files, or a whole directory (recursive) in one pass, with a per-file breakdown
  and one aggregate mutation score.

**Running your tests**
- **GdUnit4 runner** — reads GdUnit4's JUnit XML.
- **Exit-code runner** (`--runner command`) — any headless harness that exits non-zero on failure;
  no GdUnit4 addon needed.

**Reports**
- Console summary with each survivor as `file:line:col` + the swap and a `→ kill it` hint.
- Stryker [`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements)
  JSON (`--json`) and a ready-to-open HTML report (`--html`).

**Fitting your project**
- Test suites are skipped by default; `--exclude` globs drop anything else.
- `.gdmutant.toml` persists per-project flags.
- `--dry-run` lists mutants without running Godot at all.
- Mutates in place and restores after every mutant and on exit.

## Requirements

| | |
|---|---|
| **Godot** | 4.4+ (only for real runs; `--dry-run` needs no Godot) |
| **Python** | 3.12+ |
| **GdUnit4** | optional — only for the GdUnit4 runner; the exit-code runner needs no addon |

## Install

gdmutant is a dev tool, not a runtime dependency. Install it into your project from git at a
**pinned commit** (it is not on PyPI yet):

```sh
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
uv run gdmutant run path/to/module.gd --project . \
  --runner command --command "godot --headless --script res://tests/run_tests.gd"
```

If your Godot project has no Python, keep gdmutant in a tiny **non-package** uv project beside your
game (e.g. under `devtools/`) so it never touches shipped code — a `pyproject.toml` with
`[tool.uv] package = false` and a `.python-version` pinning 3.12+, then `uv add` there. Once
published, `pipx install gdmutant` will be the one-liner.

## Quickstart

Clone the repo and install the pinned toolchain to hack on gdmutant itself:

```sh
mise install       # the pinned Python + uv  (or install uv yourself and skip this)
uv sync --frozen   # the exact locked dependencies
```

**See it work without Godot** — list the mutants for the bundled fixture:

```sh
uv run gdmutant run corpus/turn_order.gd --dry-run
```

**Run it for real on the bundled corpus** — the corpus ships a tiny exit-code harness, so you get a
real mutation score with only **Godot** on your machine (no addon, nothing to vendor):

```sh
uv run gdmutant run corpus/turn_order.gd --project corpus \
  --runner command --command "godot --headless --path . --script res://harness/run_tests.gd"
```

It reruns the suite against all 18 mutants and reports **~61% killed, 7 survivors** — the fixture is
deliberately under-tested (with a couple of equivalent mutants), so a real run surfaces live
survivors, exactly as mutation testing does on real code. (On macOS, use the app-bundle path to
`godot` inside `--command`.)

**Run it on your own project** — with the [GdUnit4](https://github.com/godot-gdunit-labs/gdUnit4)
addon under `res://addons/gdUnit4/`, the default runner reads its JUnit XML:

```sh
uv run gdmutant run path/to/module.gd --project path/to/godot-project --json report.json
```

## Example output

```
18 mutants for corpus/turn_order.gd:
  corpus/turn_order.gd:8:17   comparison   > -> >=
  corpus/turn_order.gd:13:11  comparison   < -> <=
  corpus/turn_order.gd:13:13  numeric      0 -> 1
  ...
  corpus/turn_order.gd:27:15  boolean      and -> or
  corpus/turn_order.gd:27:19  logical-not  not -> (deleted)
  corpus/turn_order.gd:32:9   constant     true -> false
```

A real run adds a killed/survived verdict per mutant, a mutation score, and each survivor with a
`→ kill it` hint. New to the output?
See [reading your first report](docs/reading-your-first-report.md).

## Configuration

Drop a `.gdmutant.toml` in the directory you run gdmutant from to persist per-project defaults; an
explicit CLI flag always overrides the file.

```toml
project = "."
runner = "command"
command = "godot --headless --script res://tests/run_tests.gd"
# godot = "/Applications/Godot.app/Contents/MacOS/Godot"   # (GdUnit4 runner; macOS app-bundle path)
# tests = "res://test"
# report-path = "reports/report_1/results.xml"
# timeout = 60
# require-clean = true
# exclude = ["*_generated.gd", "*/vendor/*"]
```

| Flag | Purpose |
|---|---|
| `--runner gdunit4\|command` | read GdUnit4's JUnit XML, or judge by exit code |
| `--exclude '<glob>'` | skip files on a directory target (repeatable; adds to the config list) |
| `--timeout <s>` | per-mutant timeout (default: 10× the baseline run, floored 10s, capped 600s) |
| `--report-path` | where the project writes GdUnit4's JUnit XML |
| `--require-clean` | refuse to run with uncommitted changes (default: warn only) |

**Test suites are skipped automatically.** A directory target leaves your tests out of the mutation
set — gdmutant skips anything under a `test/`/`tests/` folder, named `test_*.gd` / `*_test.gd` /
`*Test.gd`, or extending `GdUnitTestSuite` / `GutTest`, the same way StrykerJS and cargo-mutants do.
Naming a file explicitly on the command line always mutates it; the skip and `--exclude` only narrow
a directory expansion. `--dry-run` shows exactly what survives the filter.

## Reports

`--html report.html` writes a self-contained page — the standard mutation-testing-elements viewer
with the report inlined (the viewer loads from a pinned CDN, so rendering needs network; saving
does not):

```sh
uv run gdmutant run path/to/module.gd --project . --html report.html
```

The `--json` output is the standard Stryker
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema,
so it renders in that ecosystem's interactive viewer and, once the repo is public, can be hosted on
the free [Stryker Dashboard](https://dashboard.stryker-mutator.io) for a mutation-score badge.
See [reading your first report](docs/reading-your-first-report.md) to wire up a viewer yourself.

## Continuous integration

gdmutant answers one question a green build can't: *do the tests actually bite?* It is an
**advisory** signal — report-mode, never a hard gate — complementary to coverage. A minimal
workflow installs Godot, runs gdmutant against one module through your headless harness, and uploads
the report (the same invocation gdmutant's own self-test pins against real Godot):

```yaml
# .github/workflows/mutation.yml
name: Mutation testing (advisory)
on: [pull_request]
jobs:
  gdmutant:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: chickensoft-games/setup-godot@v2
        with:
          version: 4.4.0        # your project's Godot version
          use-dotnet: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/kphutt/gdmutant@main   # (pip install gdmutant, once published)
      - run: |
          gdmutant run path/to/module.gd --project . \
            --runner command --command "godot --headless --script res://tests/run_tests.gd" \
            --json report.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: mutation-report
          path: report.json
```

Swap the `--runner command` line for the default GdUnit4 runner if your suite uses GdUnit4. Keep it
advisory (no `needs:` gate) until you've triaged the first survivors.

## Safety

gdmutant mutates the source file **in place**, restoring it after each mutant and on a normal exit
or Ctrl-C. A hard kill (SIGKILL, power loss) could still leave one swap on disk, and an open Godot
editor may hot-reload mid-run — so commit or stash before a run. gdmutant **warns** on uncommitted
changes; `--require-clean` makes that a hard stop.

## How it works

The engine is a language-neutral loop — select → mutate → run tests → tally killed/survived →
mutation score — with two language-specific pieces behind an adapter: mutating the AST (via
gdtoolkit) and running the tests (the GdUnit4 or exit-code runner). A new language is one small
adapter. The full design is in [`docs/design/DESIGN.md`](docs/design/DESIGN.md).

## Documentation

- [Reading your first report](docs/reading-your-first-report.md) — verdicts, kill hints, equivalent mutants.
- [Design & architecture](docs/design/DESIGN.md) — the engine, requirements, and the "Saboteur & the Jury" design.
- [Roadmap](ROADMAP.md) — what's done and what's next.
- [Driving gdmutant from an AI agent](docs/agent-guide.md) — the invocation, JSON schema, exit-code contract, and survivor→killing-test loop.

## Contributing

Contributions are welcome — see [AGENTS.md](AGENTS.md) for the toolchain, conventions, and local
checks (`ruff` / `mypy` / `pytest` at 100% coverage).

## License

[MIT](LICENSE) — © 2026 Karsten Huttelmaier. Third-party licenses are logged in
[CREDITS.md](CREDITS.md).
