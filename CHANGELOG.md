---
type: record
status: active
created: 2026-07-18
---

# Changelog

All notable changes to gdmutant are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — unreleased

gdmutant mutates real GDScript and reports survivors end-to-end via the standalone `gdmutant run`
CLI.

### Added

- AST-based mutation of GDScript via [gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit),
  with a re-parse validity guard so invalid mutants are never run.
- Operators: comparison, boolean, arithmetic, constant, numeric-literal, compound assignment,
  modulo, unary-not, and statement-deletion.
- Generation-time exclusions for token positions the language itself rules out as meaningful
  mutants, so they never reach the report as survivors: a `%` used for string formatting, a `+` that
  is string concatenation (GDScript's `String` defines no `-`), and a property declaration's
  initializer whose stored value no getter can read back.
- Framework-agnostic test runners over one shared runner contract (a runner-agnostic adapter seam):
  **GdUnit4** and **GUT** as first-class peer JUnit-XML adapters (`--runner gdunit4` / `--runner gut`,
  neither privileged in the engine), plus an exit-code runner (`--runner command`) for any headless
  harness without JUnit output — no addon required. Every runner upholds a crash-safety guarantee: a
  load/compile crash surfaces as a kill or error, never a silent zero-test pass (GUT's empty-report
  case is caught explicitly as `tests == 0` → error).
- Multi-file and directory targets: mutate several files or a whole directory in one pass with a
  per-file breakdown and one aggregate mutation score.
- `--jobs N` runs N mutants in parallel, each on its own copy of the project so in-place mutation
  can't collide — same verdicts as a serial run (process isolation; the per-mutant timeout is scaled
  by N so contention can't cause a false timeout), just faster (measured ~3× at `--jobs 4` on a real
  GdUnit4 module).
- Test suites are skipped by default on directory targets (by `test/`/`tests/` folder, `test_*.gd` /
  `*_test.gd` / `*Test.gd` name, or `extends GdUnitTestSuite` / `GutTest`), with an `--exclude` glob
  (and a `.gdmutant.toml` `exclude` list) to skip anything else.
- Reports: a console survivor summary that explains each gap (what's untested, why it matters, where
  to start a test), the
  [`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) JSON
  schema (`--json`), and a genuinely self-contained HTML report (`--html`) — one file with every
  style, script and image inlined, so it opens with no network at all and works as a CI artifact or
  an email attachment. It marks the exact changed characters in your source, groups mutants into
  **findings** (one spot, one operator — the unit of work a single test closes), and inlines the
  per-operator survivor reference so an offline reader can still look up what an operator means. A
  multi-file run opens on a file index ordered by survivors. Replaces an earlier page that inlined
  the report JSON but loaded the generic viewer from a CDN, and so rendered blank offline.
- Every finding in the HTML report has an **address** — `path:line:column:operator`, the tuple it
  was grouped by, so it is the same string every time the report is regenerated from source that has
  not moved. The selected finding lives in the URL, so a reload keeps your place and "look at this
  survivor" is a link you can send. A link that no longer resolves falls back to the file it named,
  or to the file index, and never to the wrong finding.
- Findings can be **marked done** as you work through them, with a "k of n done" count. The marks
  live in your browser, for that report file, so a copy that travels opens unmarked rather than
  showing someone else's progress. A mark made against an earlier run of a finding that is *still
  surviving* is flagged "re-check" and is **not** counted as done — a stale tick must never hide a
  live survivor.
- `.gdmutant.toml` for persisted per-project flags; `--dry-run` to list mutants without running
  Godot.
- Live self-test against real Godot in CI, pinning both runner paths to exact per-mutant outcomes.
- A GitHub Action, so a consumer's CI runs gdmutant from a few lines of `uses:` YAML instead of a
  hand-rolled install step: it sets up Python and Godot, installs gdmutant, runs it against the
  consumer's project, and writes the surviving mutants — with their explanations — to the job
  summary, right where a reviewer already looks.

### Fixed

- The "executable not found" message is **mode-aware**. Under `--runner command` the executable
  comes from the `--command` string, not from `--godot` — so the message now says that, states that
  `--godot` has no effect in that mode, and shows the user's own command back with the path slot
  marked. It previously recommended `--godot`, which that mode does not read: setting it returned
  the byte-identical error.
- `--runner command` says up front when the project has no Godot import cache (`.godot/`). On a
  fresh checkout Godot imports every asset before it will run anything — minutes of silence that
  reads as a hang. The JUnit runners do that warm-up themselves (and the "preparing the project"
  notice now says it can take minutes); the exit-code runner cannot, so it names the one command
  that fixes it instead.
- Survivors that are **unkillable by where they sit** explain themselves, instead of handing out
  advice nobody can follow. Two places qualify:
  - **inside an `assert`** — a failed assert aborts the Godot process, so no in-process test can
    pass on the original and fail on the mutant. On defensive code these can be most of a file's
    survivors.
  - **on an `enum` member's value** — code that names the member moves with it, so nothing observes
    the number. The generic numeric advice ("add a test at the boundary this number sets") is
    meaningless for a tag that has no boundary.

  **Nothing is skipped and no score changes.** Every one of these mutants is still generated, still
  run, and still counted. Enum values are deliberately *not* suppressed: bitflag enums and any enum
  that is serialised have values that really are read as numbers, and gdmutant reads one file at a
  time, so it cannot see a numeric use in another file, a save format, or engine code. Suppressing
  them would hide exactly the bugs that matter most. The reference explains what would make one
  killable and leaves the call to you.

  Every surface agrees — the console block, the JSON report, the HTML page and the job summary all
  resolve their explanation and their link through one rule, so the page can never offer an
  operator's reference beside a contradicting explanation. The `statement-deletion` reference also
  now names the **redundant initializer** (`_cells = PackedByteArray()` where the declaration
  already default-initialises it) as its commonest legitimate equivalent, with the check that
  confirms one.
- **Progress is measured, not predicted.** The up-front `estimated ≈ 24s` figure is gone. It was
  wrong in both directions at once — 1.7–3.4× *under* on a real project (it counted neither
  gdmutant's own per-mutant work nor timeouts, which were four minutes of one 6m24s run) and, since
  it never took `--jobs` into account, roughly N× *over* under `--jobs N`. In its place:
  - **before**, the facts: `18 mutants to run. Baseline suite 1.4s; each mutant is capped at 30s.`
    The cap is the part that paces the wait — it says how long silence is normal.
  - **during**, a heartbeat: `… 7/18 done in 1m 12s — 2 survived, 1 timed out.` Every 30s on a
    terminal; every 60s or 10% of mutants (whichever is rarer) in a log or under `CI=true`, and
    always once at the end of each file. New `--progress {auto,plain,none}` overrides the choice.
  - **after**, the wall-clock every other test runner prints and gdmutant did not:
    `Done in 6m 32s — 18 mutants, 8 timed out (4m 0s of that). Baseline suite 1.4s.` The timeout
    cost is broken out because it is the cost nobody can see.

  No finish time is forecast anywhere. One was built and measured against a real Godot project
  first: on an even workload it tracked the true finish to within 5%, but on the shape that
  actually matters — hanging mutants arriving after the rate has settled — it read 3.2s at 25%
  done for a run that took 58.0s, so it was dropped.

### Safety

- Mutations are applied in place and restored after each mutant and on exit; gdmutant warns on
  uncommitted changes and `--require-clean` makes that a hard stop.
