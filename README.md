# gdmutant

[![CI](https://github.com/kphutt/gdmutant/actions/workflows/ci.yml/badge.svg)](https://github.com/kphutt/gdmutant/actions/workflows/ci.yml)

> **`gdmutant` is a provisional codename**, not yet cleared for public use. It lives in one
> place: this README + the repo name.

**A mutation-testing tool — the first *usable* one for GDScript/Godot, built to be language-agnostic.** Point it at a
GDScript module with a test suite; it mutates the source (flip `>`↔`>=`, `and`↔`or`, bump a number, …), reruns
the tests per mutant, and reports **survivors** — lines a bug could live on and no test would catch. Coverage
says a line *ran*; mutation says a bug there would be *caught*. That gap is the product.

## Why this exists (the opening)
- **No *usable* mutation tester exists for GDScript.** [Stryker](https://stryker-mutator.io/) does JS/TS,
  C#, Scala; mutmut does Python; PIT does Java. GDScript has only a **dormant, unlicensed proof-of-concept**
  ([hanse7962/GodotMutationTesting](https://github.com/hanse7962/GodotMutationTesting) — a few weeks of work
  in Apr–May 2026, no README, no license), so the space for a documented, adopted tool is open. And the AST
  work is nearly free now that
  [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit) ships a real GDScript parser.
- **AI just opened the demand.** When an AI writes the tests, coverage is *especially* a lie (models write
  tests that pin the code they just wrote). Mutation is one of the few **executable, model-independent**
  signals that a test actually bites. So under-tested ecosystems like game-dev suddenly need this.
- **Extracted from real use, not built speculatively.** The driver is `project-rampart` (a Godot roguelike)
  needing to trust its two hardest systems (turn scheduler + procgen connectivity). This tool gets *extracted
  from that need* and dogfooded on it.

## Prior art, licenses & why build (not extend)
- **Can't build on the GDScript POC.** `hanse7962/GodotMutationTesting` is **unlicensed** (all rights
  reserved) — legally untouchable, and a dormant undocumented experiment anyway. Study for ideas only.
- **No pluggable engine to "just add GDScript to."** Stryker is a *family of separate per-language tools*
  (StrykerJS/.NET/Scala), not one core with a language-plugin API — adding GDScript ≈ writing a whole new
  Stryker. The one deliberately language-extensible tool, `universalmutator`, is **regex-based** (text-rule
  mutation → mostly-invalid mutants; worse than AST) and academic. So there's no good host to extend —
  a thin engine + a gdtoolkit AST adapter is genuinely the best path, not reinvention-for-its-own-sake.
- **The mature tools are permissive → learn freely.** Stryker/PIT/Mull **Apache-2.0**; mutmut/infection
  **BSD-3**; cosmic-ray/cargo-mutants **MIT**. Patterns aren't copyrightable and these even allow adapting
  code with attribution — so steal the architecture (coverage-guided selection, schemata, incremental, the
  report schema) openly.


- **This tool → MIT** (matches the norm; maximally adoptable).

## Patterns to steal (cross-language prior art)
- **Split mutator (AST) / runner (tests) / reporter** — the generic-engine + adapters design; every good
  tool does this (mutmut, Stryker, mutant).
- **Coverage-guided mutant selection** — only run tests that cover the mutated line. The #1 speedup (PIT, Stryker).
- **Mutant schemata / "switching"** — bake all mutants into one instrumented copy, toggle via a switch,
  avoid re-parsing/re-running per mutant (PIT).
- **Incremental / diff-scoped** — mutate only changed lines (the per-PR mode).
- **Adopt Stryker's [mutation-testing-elements](https://github.com/stryker-mutator/mutation-testing-elements)
  report schema** — then output renders in the existing HTML viewer for free.
- Study: [awesome-mutation-testing](https://github.com/theofidry/awesome-mutation-testing),
  [mutation-testing-in-patterns](https://github.com/atodorov/mutation-testing-in-patterns). Closest engine to
  copy the shape of: **mutmut** (Python + AST, like ours).

## Design goals
- **Ship fast.** A working v0.1 that mutates one real module and prints survivors beats a perfect framework.
- **Standalone. Usable by anyone — no AI required.** A normal CLI a developer installs and runs,
  exactly like Stryker is in its domain. AI is *optional upside* (see modes), never a dependency. This is the
  #1 design constraint: **a non-AI developer must be able to pick it up and use it from the README alone.**
- **Generic engine, per-language adapters.** The loop (mutate → run tests → killed/survived → report) is
  identical in every language; only two bits are language-specific — mutating the AST, and running that
  language's tests. Build the loop once; a new language = one small adapter.
  - **GDScript adapter first** (the gap; via gdtoolkit's parser).
  - **TypeScript:** don't compete with Stryker — *delegate* to it, or skip. Adapters are independent.

## Architecture (as built)
```
gdmutant/
  engine/          language-neutral loop: select → mutate → run → tally → mutation score
    operators/     operator catalog (comparison/boolean/arithmetic/constant/numeric + compound-assignment/modulo/not)
    spans.py       AST-guided source-span editing (docs/decisions/0002)
    runner.py      the Runner interface + JUnit-XML parsing
    report.py      Stryker mutation-testing-elements JSON + a console summary
  adapters/
    gdscript/      gdtoolkit AST → locate token → mutate → NF-5 re-parse guard; the GdUnit4 runner
  cli.py           the standalone `gdmutant run` entry point (no AI required)
corpus/            a real GDScript fixture module + GdUnit4 suite (intentionally under-tested — and with a few equivalent mutants — so a real run surfaces live survivors, as real mutation testing does)
```
Two modes, one engine: a **deterministic operator core** (reproducible — the mode a CI check can trust) and,
later, an optional **LLM-semantic mode** (plausible-bug mutants: off-by-one, dropped-last-element, swallowed
error) for *hardening*, kept out of the gate because it's nondeterministic.

## Status
**v0.1 works — gdmutant mutates real GDScript and reports survivors end-to-end.** From a `.gd` file it
generates AST-based mutants (comparison / boolean / arithmetic / constant / numeric-literal, plus
compound-assignment / modulo / unary-not / statement-deletion), runs the
project's GdUnit4 suite per mutant, classifies killed / survived / timeout / invalid / error, computes a
mutation score, and emits a console summary + a Stryker `mutation-testing-elements` JSON report — via the
standalone `gdmutant run` CLI (no AI required). Proven end-to-end on the bundled `corpus/` module; the
**live `godot --headless` + GdUnit4** invocation is pending CI validation (see `ROADMAP.md`), so the
package stays version `0.0.0` until that lands, then tags `0.1.0`. Spun off from `project-rampart` (a
Godot roguelike) so it has its own home.

## Quickstart
Clone the repo, then install the pinned toolchain + locked deps:
```sh
mise install       # installs the pinned Python + uv  (or install uv yourself, then skip this)
uv sync --frozen   # installs the exact locked dependencies
```

**See it work without Godot** — list the mutants gdmutant generates for the bundled fixture:
```sh
uv run gdmutant run corpus/turn_order.gd --dry-run
```
```
18 mutants for corpus/turn_order.gd:
  corpus/turn_order.gd:8:17  comparison  > -> >=
  corpus/turn_order.gd:13:11  comparison  < -> <=
  corpus/turn_order.gd:13:13  numeric  0 -> 1
  ...
  corpus/turn_order.gd:27:15  boolean  and -> or
  corpus/turn_order.gd:27:19  logical-not  not -> (deleted)
  corpus/turn_order.gd:32:9  constant  true -> false
```

**...then run it for real on the bundled corpus — no addon needed.** The corpus ships a tiny
exit-code test harness, so you can go straight from the dry-run to a real mutation score with only
**Godot** on your machine (no GdUnit4 install, nothing to vendor):
```sh
uv run gdmutant run corpus/turn_order.gd --project corpus \
  --runner command --command "godot --headless --path . --script res://harness/run_tests.gd"
```
It reruns the corpus suite against all 18 mutants and reports **~61% killed, 7 survivors** — the
fixture is deliberately under-tested (with a couple of equivalent mutants), so a real run surfaces
live survivors, exactly as mutation testing does on real code. (macOS: put the app-bundle path to
`godot` inside `--command`.) This is the whole pipeline end-to-end in one command.

**Run the real thing** — needs Godot 4.4+ and the [GdUnit4](https://github.com/godot-gdunit-labs/gdUnit4)
addon installed in the target project (under `res://addons/gdUnit4/`):
```sh
uv run gdmutant run path/to/module.gd --project path/to/godot-project [--json report.json]
```
For each mutant it reruns the project's GdUnit4 suite, prints the survivors (`file:line:col` + the swap,
each with a `→ kill it` hint) with a mutation score, and optionally writes a `mutation-testing-elements`
JSON report (`--json report.json`, or `--json -` to stream it to stdout). (Once published: `pipx install
gdmutant`.) New to the output? [`docs/reading-your-first-report.md`](docs/reading-your-first-report.md)
walks through survivors, the kill hints, and equivalent mutants.

> **Trying the GdUnit4 runner on the *bundled corpus*?** The corpus doesn't vendor the addon, so
> fetch it first — `scripts/install-gdunit4.sh` (the same pinned install CI uses) drops it into
> `corpus/addons/gdUnit4/` — then run with `--project corpus --runner gdunit4 --godot <godot>`. Or
> skip the addon entirely and use the exit-code demo above.

> **macOS:** the `--godot` flag applies to the **GdUnit4 runner**, which launches Godot itself. Godot
> ships as an app bundle and is never on your PATH, so point `--godot` at the binary inside it:
> `--godot /Applications/Godot.app/Contents/MacOS/Godot` (gdmutant tells you this if it can't find
> Godot). With `--runner command` gdmutant does **not** launch Godot — your `--command` does — so you
> only need `godot` to resolve inside that command (on PATH, or an absolute path in the command).

**No GdUnit4?** For a project with a hand-rolled headless test harness (like `project-rampart`'s),
use the exit-code runner instead — any command that exits non-zero on failure works, no JUnit XML
needed:
```sh
uv run gdmutant run path/to/module.gd --project path/to/godot-project \
  --runner command --command "godot --headless --script res://tests/run_tests.gd"
```
See [`docs/decisions/0005`](docs/decisions/0005-exit-code-test-runner-convention.md) for the
convention (and its coarser killed/errored resolution vs GdUnit4's XML).

Other flags: `--report-path` if your project writes GdUnit4's JUnit XML somewhere other than the
default `reports/report_1/results.xml`, and `--timeout` (seconds, per mutant — by default *derived
from the baseline run*: 10× its wall-clock, floored at 10s and capped at 600s, so a hanging mutant is
caught in seconds; pass an explicit value to override). `gdmutant run --help` lists them all.

**Stop retyping flags — `.gdmutant.toml`.** Drop a `.gdmutant.toml` in the directory you run gdmutant
from to persist the per-project defaults; an explicit CLI flag always overrides the file. Keys mirror
the flag names:
```toml
project = "."
runner = "command"
command = "godot --headless --script res://tests/run_tests.gd"
# godot = "/Applications/Godot.app/Contents/MacOS/Godot"   # (GdUnit4 runner)
# tests = "res://test"
# report-path = "reports/report_1/results.xml"
# timeout = 60
# require-clean = true
```
Then `gdmutant run path/to/module.gd` picks them up. (`source`, `--json`, and `--dry-run` stay on the
command line — they're per-invocation, not project settings.)

> **Your code is safe, but commit first.** gdmutant mutates the source file **in place**, restoring
> it after each mutant and on a normal exit or Ctrl-C — but a hard kill (SIGKILL / power loss) could
> leave one swap on disk, and an open Godot editor may hot-reload mid-run. So commit or stash before
> a run. gdmutant **warns** if the target has uncommitted git changes; pass `--require-clean` to make
> that a hard stop instead.

> The live `godot --headless` path is validated end-to-end in CI against **real Godot** — both the
> GdUnit4 and exit-code runners, pinned to exact per-mutant outcomes (`tests/test_selftest_live.py`).
> `--dry-run` still needs no Godot.

**Want a page you can just open? `--html report.html`.** gdmutant writes a ready-to-open HTML report
— the standard mutation-testing-elements viewer with the report inlined (the viewer itself loads from
a pinned CDN, so rendering needs network; saving doesn't). One file, double-click it:
```sh
uv run gdmutant run path/to/module.gd --project . --html report.html
```

**Prefer to wire it yourself (or keep the report and page separate)?** The `--json` output is the
standard Stryker
[`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) schema,
so it renders in that ecosystem's interactive viewer. Save this next to your `report.json` as
`view.html`:
```html
<mutation-test-report-app></mutation-test-report-app>
<script src="https://www.unpkg.com/mutation-testing-elements@3.8.4"></script>
<script>
  fetch("report.json")
    .then((r) => r.json())
    .then((report) => (document.querySelector("mutation-test-report-app").report = report));
</script>
```
then serve the folder and open it (`python3 -m http.server` → visit `view.html`) for a
source-highlighted, survivor-by-survivor view. Once the repo is public, the free
[Stryker Dashboard](https://dashboard.stryker-mutator.io) can also host the report and produce a
mutation-score **badge** — all from the JSON gdmutant already emits.

**Driving gdmutant from an AI agent?** See [`docs/agent-guide.md`](docs/agent-guide.md) — the exact
invocation, the JSON schema, the `0`/`1`/`2` exit-code contract, the "never leaves your tree mutated"
guarantee, and the survivor→killing-test loop, in one read.

## Install into your project (local, no clone)
The Quickstart above is for hacking on gdmutant itself. To run it against **your own** Godot project,
install it as a dev-tool dependency — no clone needed. It's not on PyPI yet, so install from git at a
**pinned commit** (not a moving branch):
```sh
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
uv run gdmutant run path/to/module.gd --project . \
  --runner command --command "godot --headless --script res://tests/run_tests.gd"
```

**A Godot project with no Python?** gdmutant is a dev tool, not a runtime dependency — keep it in a
tiny **non-package** uv project beside your game so it never touches your shipped code. Add a
`pyproject.toml` (e.g. under a `devtools/` dir):
```toml
[project]
name = "yourgame-devtools"
version = "0"
requires-python = ">=3.12"   # gdmutant's floor — see below
dependencies = []

[tool.uv]
package = false              # a workspace of tools, not an installable package
```
then `uv add "git+https://github.com/kphutt/gdmutant@<sha>"` there.

**Pin your Python.** gdmutant supports **Python 3.12+**. uv resolves to whatever interpreter it finds
otherwise — e.g. a system CPython 3.14 rather than your project's pinned 3.12 — so set a
`.python-version` (or an equivalent `mise`/`asdf` pin) next to that `pyproject.toml` to choose it
deliberately, and keep `requires-python` in sync.

## Next steps
1. ✅ **Repo hardened + stack chosen.** Security baseline + Python CI (ruff / mypy / pytest+coverage /
   pip-audit, plus a gitleaks secret-scan). The engine is **Python + uv + gdtoolkit** (see
   `docs/decisions/0001`), with **GdUnit4** as the first test-runner adapter.
2. **Name — availability checked, clearance pending.** `gdmutant` is free on PyPI, npm, and GitHub as of
   this writing; the final name + a trademark sense-check are settled before public launch (see the
   provisional-codename note up top).
3. ✅ **`DESIGN.md` design gate written + reviewed** — goals, FG/NF requirements, the "Saboteur & the
   Jury" architecture, and the build plan (`docs/design/DESIGN.md`).
4. ✅ **v0.1 built against the bundled `corpus/` fixture** — engine loop, operator catalog, GDScript
   adapter (NF-5 guard), GdUnit4 runner, Stryker reporter, and the `gdmutant run` CLI. Mutates
   `corpus/turn_order.gd` (18 mutants) and prints survivors end-to-end.
5. ✅ **Live CI Godot validation** — both runner paths run against **real Godot** in CI, pinned to
   exact per-mutant outcomes (`tests/test_selftest_live.py`, `scripts/install-gdunit4.sh`). This
   caught two real runner bugs (headless-mode flag, relative project path).
6. **Remaining before a public launch** (see `ROADMAP.md`): the statement-deletion operator and the
   pre-public checklist, then flip the repo public — never launch empty.

## Where it fits in your CI
This tool answers one question a green CI build can't: *"do the tests actually bite?"* It's an **advisory**
signal — report-mode, never a hard gate — complementary to coverage, run alongside whatever review and
CI a project already has. It shares no code with any reviewer tool; it's a standalone CLI on purpose.

**Use in your CI.** A minimal advisory workflow — installs Godot, runs gdmutant against one module
via your headless harness, and uploads the report. (This is the same invocation gdmutant's own
self-test pins against real Godot, so it's known to work.)
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
Swap `--runner command --command "…"` for the default GdUnit4 runner (`--godot godot`) if your suite
uses GdUnit4. Keep it advisory (no `needs:` gate) until you've triaged the first survivors.

## License
[MIT](LICENSE) — © 2026 Karsten Huttelmaier. Third-party licenses are logged in [CREDITS.md](CREDITS.md).
