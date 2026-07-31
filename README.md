<h1 align="center">gdmutant</h1>
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
test catches.

Coverage tells you a line *ran*; mutation tells you a bug there would be *caught* — a gap that
widens when an AI writes the tests, since models tend to pin code they just wrote. A standalone
CLI, no AI required.

## Is this for you?

- You write **GDScript** and test with **GUT**, **gdUnit4**, or any `godot --headless` command.
- You already have a test suite and want to know which of it actually bites — gdmutant grades your
  tests, it doesn't replace them.

## Quickstart

Not on PyPI yet — install from git at a pinned commit. New `uv` project? Name the Python version
explicitly — `uv init` alone floors on whatever interpreter it finds, and gdmutant needs 3.12+:

```sh
uv init --python 3.12
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
```

No Python in your game repo? Keep gdmutant in a tiny non-package uv project beside it
(`[tool.uv] package = false`).

**No Godot needed yet** — `--dry-run` lists the mutants gdmutant would generate, with no test run:

```sh
uv run gdmutant run corpus/turn_order.gd --dry-run
```

**For real, pick your runner.** [GUT](https://github.com/bitwes/Gut) and
[gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4) are peer JUnit-XML readers (gdUnit4 is the
default):

```sh
uv run gdmutant run path/to/module.gd --project . --runner gut --json report.json   # GUT
uv run gdmutant run path/to/module.gd --project . --json report.json                # gdUnit4 (default)
```

**No JUnit XML?** Point the exit-code runner at any headless command that exits non-zero on
failure:

```sh
uv run gdmutant run path/to/module.gd --project . \
  --runner command --command "godot --headless --script res://tests/run_tests.gd" --json report.json
```

Godot exits `0` even on a harness that fails to *compile* — gate it on `can_instantiate()`, or a
broken mutant reads as a false pass.

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

## Example output

A real run against the bundled `corpus/turn_order.gd` fixture (18 mutants, gdUnit4 runner):

```sh
uv run gdmutant run --project corpus --godot /path/to/godot corpus/turn_order.gd
```

```
Mutation score: 61.1%
  killed:   11
  timeout:  0  (counted as killed)
  survived: 7
  ignored:  0  (suppressed, excluded from score)
  invalid:  0
  error:    0

Survivors (7):

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

... 6 more survivors follow, one block each, same format.
```

**Mutation score isn't a target — it's a direction.** There's no universal "good" number; watch it
trend as you kill survivors, not the absolute value. `ignored`, `invalid`, and `error` are the other
three result categories — see the [survivor reference](docs/survivors/README.md) for what each
means, the exact formula, and every operator's kill/equivalent guidance.

## The workflow

1. **Pick a target.** Start with the file you trust least — new code, thin tests, or core logic
   (state machines, scoring, boundaries). Run one file at a time; a whole directory's survivor list
   is too much to work through at once.
2. **Run it** (see Quickstart above).
3. **Kill or annotate every survivor.** Each block's `start` line says where to add a test. A
   proven equivalent — one that truly can't change behavior, like an unreachable clamp boundary —
   gets `# gdmutant: ignore` on that line (`ignore[comparison]` for one operator) plus a reason
   instead; see the [survivor reference](docs/survivors/README.md) for each operator's cases.
4. **Re-run to confirm**, then **move on** — done with a file at zero `survived`. No third state,
   so the loop always ends.

**Runtime.** gdmutant boots Godot once per mutant, so time scales with mutant count — it says so up
front:

```
18 mutants; baseline ~1.4s each → estimated ≈ 24s
```

`--jobs N` runs N mutants at once, each on its own project copy. The speedup is real but sub-linear
— measured ~3× at `--jobs 4` on a real GdUnit4 module — because every worker copies the project.

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

## How it works

A language-neutral loop — select → mutate → run → tally → score — with two language-specific
pieces behind an adapter: mutating the AST (via
[gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)) and running the tests. A new language
is one small adapter.

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
