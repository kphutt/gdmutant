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
  is string concatenation (GDScript's `String` defines no `-`), a `+=` that appends to a string (same
  reason), and a property declaration's initializer whose stored value no getter can read back.
  These change the mutation score, and upward: an excluded mutant leaves the denominator instead
  of counting as a survivor. That is deliberate and is the honest direction: the language already
  settles all four, so none is a gap a test could ever have closed. The first three are invalid
  GDScript, and the property initializer is valid GDScript whose mutant is inert, because the value
  it stores can never be read back. It does mean, though, that a score is not comparable across a
  version that added an exclusion. Each is recognised from the parse tree only
  where the shape is certain (a `String`-typed *variable* is still mutated), because reporting noise
  is a smaller failure than hiding a real gap. `docs/survivors/README.md` states the full list and
  its effect on the score for users.
- Framework-agnostic test runners over one shared runner contract (a runner-agnostic adapter seam):
  GdUnit4 and GUT as first-class peer JUnit-XML adapters (`--runner gdunit4` / `--runner gut`,
  neither privileged in the engine), plus an exit-code runner (`--runner command`) for any headless
  harness without JUnit output, no addon required. Every runner upholds a crash-safety guarantee: a
  load/compile crash surfaces as a kill or error, never a silent zero-test pass. A whole-run
  `tests == 0` check catches the empty-report shape, but GUT needs more: it skips a suite that fails
  to compile, runs the rest green, and exits 0, so the adapter also treats any run whose test count
  drops below the healthy baseline's as an error, with a non-determinism canary that warns (never
  errors) when a later run reports more tests than the baseline.
- Multi-file and directory targets: mutate several files or a whole directory in one pass with a
  per-file breakdown and one aggregate mutation score.
- `--jobs N` runs N mutants in parallel, each on its own copy of the project so in-place mutation
  can't collide: same verdicts as a serial run (process isolation, the per-mutant timeout is scaled
  by N so contention can't cause a false timeout), just faster (measured ~3× at `--jobs 4` on a real
  GdUnit4 module).
- Test suites are skipped by default on directory targets (by `test/`/`tests/` folder, `test_*.gd` /
  `*_test.gd` / `*Test.gd` name, or `extends GdUnitTestSuite` / `GutTest`), with an `--exclude` glob
  (and a `.gdmutant.toml` `exclude` list) to skip anything else.
- Reports: a console survivor summary that explains each gap (what's untested, why it matters, where
  to start a test), the
  [`mutation-testing-elements`](https://github.com/stryker-mutator/mutation-testing-elements) JSON
  schema (`--json`), and a genuinely self-contained HTML report (`--html`): one file with every
  style, script and image inlined, so it opens with no network at all and works as a CI artifact or
  an email attachment. It marks the exact changed characters in your source, groups mutants into
  findings (one spot, one operator, the unit of work a single test closes), and inlines the
  per-operator survivor reference so an offline reader can still look up what an operator means. A
  multi-file run opens on a file index ordered by survivors. Replaces an earlier page that inlined
  the report JSON but loaded the generic viewer from a CDN, and so rendered blank offline.
- Every finding in the HTML report has an address: `path:line:column:operator`, the tuple it
  was grouped by, so it is the same string every time the report is regenerated from source that has
  not moved. The selected finding lives in the URL, so a reload keeps your place and "look at this
  survivor" is a link you can send. A link that no longer resolves falls back to the file it named,
  or to the file index, and never to the wrong finding.
- Findings can be marked done as you work through them, with a "k of n done" count. The marks
  live in your browser, for that report file, so a copy that travels opens unmarked rather than
  showing someone else's progress. A mark made against an earlier run of a finding that is *still
  surviving* is flagged "re-check" and is not counted as done. A stale tick must never hide a
  live survivor.
- The HTML report shows each file by its project-relative path. A report is made to travel, and an
  absolute path from the machine that produced it carries that machine's username and directory
  layout into every row while telling a reader nothing they can act on. A file genuinely outside the
  project keeps its absolute path, because there is no shorter honest name for it. This changes what
  the page *displays*, and the displayed path is what a deep link and a done-mark are keyed on, so
  marks made against an earlier report of the same project do not carry over. Note the limit: the
  `--json` report and the copy of it embedded in the HTML file are both unchanged, because their
  keys are identifiers other tooling resolves. A report file still contains the absolute paths, it
  just no longer shows them.
- The embedded report is downloadable from the page, so the JSON in a report someone mailed you is
  reachable without View Source. No request and no new data, the bytes are the page's own.
- The file index sorts on any column (score, file, survived, caught, mutants), ascending or
  descending. Most survivors first stays the default, because it is the only order that answers
  "where do I start". Score would rank 1 survivor in 5 mutants level with 100 in 500.
- The header's rare-status counts are clickable, and reach the mutants behind them through the
  filter the file view already has. The three stay three things: a timeout is a kill and only a
  performance signal, a compile error is the re-parse guard working, and a runtime error is the
  actionable one, a valid mutant that ran and measured nothing because the harness fell over.
- The browser's back button returns to the file index instead of leaving the report. Structural
  moves (opening a file, going back to the index) get a history entry. Stepping between findings
  still does not, so a long file cannot bury the reader under a back press per finding.
- `.gdmutant.toml` for persisted per-project flags, and `--dry-run` to list mutants without running
  Godot.
- Live self-test against real Godot in CI, pinning both runner paths to exact per-mutant outcomes.
- A GitHub Action, so a consumer's CI runs gdmutant from a few lines of `uses:` YAML instead of a
  hand-rolled install step: it sets up Python and Godot, installs gdmutant, runs it against the
  consumer's project, and writes the surviving mutants (with their explanations) to the job
  summary, right where a reviewer already looks.

### Fixed

- Six survivor explanations stated something that is not true. The text reaches every user on every
  run, through the console block, the JSON report and the HTML report, and the docs page and the
  shipped copy of it were pinned to each other, so all three said the same wrong thing in sync.
  - Modulo blamed a clean multiple ("where `%`, `*`, and `/` can produce indistinguishable
    results"). A clean multiple is where those three differ most: `6 % 3` is 0, `6 * 3` is 18,
    `6 / 3` is 2. A reader was told the test that works cannot work. The reason is arithmetic's,
    which is what the entry's own "assert the exact result" advice already implied: nothing pins
    the result.
  - Comparison said the swapped operators "differ on exactly one input", which is false for the
    `==` ↔ `!=` pair the operator also produces, and which the entry names in its own example.
    Those two are complements and differ on every input.
  - The list of shapes that are never generated called all four "code GDScript rejects". Three
    are, resting on `String` having no `-`. The fourth, a property initializer no getter can read
    back, is valid GDScript that is merely inert, so an auditor was sent hunting a syntax error
    that is not there.
  - Numeric said "a numeric literal", but only bare decimal integers are mutated: `0.5`, `0xFF`
    and `1_000` produce nothing, so a float bound was never covered.
  - Enum member described only a `numeric` mutant on a member's value, though any mutant inside
    an `enum` block is explained there, including an arithmetic one on `A = 1 + 0`.
  - Compound assign said a string `+=` is "not mutated at all". It gets no compound-assign
    mutant, but the line still gets a statement deletion.
- `# gdmutant: ignore[statement-deletion]` no longer draws a warning that it "suppresses nothing".
  The annotation always worked. The validator checked names against the token operator catalog
  alone, and statement deletion is structural, so it lives in the GDScript adapter instead. The
  tool was contradicting its own documented advice to scope an ignore with the `mutatorName` from
  the report.
- The "executable not found" message is mode-aware. Under `--runner command` the executable
  comes from the `--command` string, not from `--godot`, so the message now says that, states that
  `--godot` has no effect in that mode, and shows the user's own command back with the path slot
  marked. It previously recommended `--godot`, which that mode does not read: setting it returned
  the byte-identical error.
- `--runner command` says up front when the project has no Godot import cache (`.godot/`). On a
  fresh checkout Godot imports every asset before it will run anything, minutes of silence that
  reads as a hang. The JUnit runners do that warm-up themselves (and the "preparing the project"
  notice now says it can take minutes). The exit-code runner cannot, so it names the one command
  that fixes it instead.
- Survivors that are unkillable by where they sit explain themselves, instead of handing out
  advice nobody can follow. Two places qualify:
  - inside an `assert`: a failed assert aborts the Godot process, so no in-process test can
    pass on the original and fail on the mutant. On defensive code these can be most of a file's
    survivors.
  - on an `enum` member's value: code that names the member moves with it, so nothing observes
    the number. The generic numeric advice ("add a test at the boundary this number sets") is
    meaningless for a tag that has no boundary.

  Nothing is skipped and no score changes. Every one of these mutants is still generated, still
  run, and still counted. Enum values are deliberately *not* suppressed: bitflag enums and any enum
  that is serialised have values that really are read as numbers, and gdmutant reads one file at a
  time, so it cannot see a numeric use in another file, a save format, or engine code. Suppressing
  them would hide exactly the bugs that matter most. The reference explains what would make one
  killable and leaves the call to you.

  Every surface agrees: the console block, the JSON report, the HTML page and the job summary all
  resolve their explanation and their link through one rule, so the page can never offer an
  operator's reference beside a contradicting explanation. The `statement-deletion` reference also
  now names the redundant initializer (`_cells = PackedByteArray()` where the declaration
  already default-initialises it) as its commonest legitimate equivalent, with the check that
  confirms one.
- Progress is measured, not predicted. The up-front `estimated ≈ 24s` figure is gone. It was
  wrong in both directions at once: 1.7–3.4× *under* on a real project (it counted neither
  gdmutant's own per-mutant work nor timeouts, which were four minutes of one 6m24s run) and, since
  it never took `--jobs` into account, roughly N× *over* under `--jobs N`. In its place:
  - before, the facts: `18 mutants to run. Baseline suite 1.4s; each mutant is capped at 30s.`
    The cap is the part that paces the wait, and it says how long silence is normal.
  - during, a heartbeat: `… 7/18 done in 1m 12s — 2 survived, 1 timed out.` Every 30s on a
    terminal, every 60s or 10% of mutants (whichever is rarer) in a log or under `CI=true`, and
    always once at the end of each file. New `--progress {auto,plain,none}` overrides the choice.
  - after, the wall-clock every other test runner prints and gdmutant did not:
    `Done in 6m 32s — 18 mutants, 8 timed out (4m 0s of that). Baseline suite 1.4s.` The timeout
    cost is broken out because it is the cost nobody can see.

  No finish time is forecast anywhere. One was built and measured against a real Godot project
  first: on an even workload it tracked the true finish to within 5%, but on the shape that
  actually matters (hanging mutants arriving after the rate has settled), it read 3.2s at 25%
  done for a run that took 58.0s, so it was dropped.

### Safety

- Mutations are applied in place and restored after each mutant and on exit. gdmutant warns on
  uncommitted changes, and `--require-clean` makes that a hard stop.
- `--require-clean` refuses anything it could not confirm, not just changes it could see. A file
  git ignores, a file outside any repository, a machine with no git, and a symlink whose target
  is unbacked all used to pass the check silently (the ignored file and the symlink being the
  worst of them, since git holds no copy of either). Without the flag, a file gdmutant cannot
  judge still says nothing. A gitignored one now warns, which it did not before, because that is
  the case gdmutant can positively tell has no copy anywhere.
- A git command that fails for a reason other than "no repository here" (dubious ownership, a
  corrupted repository) now reports what git actually said, including the fix it suggests,
  instead of a generic "not inside a git working tree". When the source is a symlink, the message
  names the file git was actually asked about, so a report about a target outside every repository
  does not read as a report about the link.
- A source file is never left half-written. Each rewrite is staged in a temporary file beside the
  target and renamed over it, so the path always holds one whole version or the other. If that
  cannot be done (no room for the temporary file, a failed flush, a lock that will not clear, or
  a file marked read-only), gdmutant stops and says so, leaving the file untouched, rather than
  attempting a write that could truncate it.
- Under `--jobs N`, a worker only ever writes inside its own copy of the project. A source file
  that is not under `--project` has no copy to mutate, so the run is refused with an explanation
  instead of writing outside the copy, which used to report every mutant as a survivor because
  the mutation never reached the project the tests ran against.
- `.gdmutant.toml` cannot decide what gdmutant executes. Its `command` and `godot` keys name a
  program to run, and the file is read from the project directory, so in a project you cloned,
  somebody else wrote it. Either key makes gdmutant refuse the whole run, with an explanation, and
  exit 2 without running anything, unless you add `--trust-config` or name the program on the
  command line yourself, which always wins over the file. The refusal fires only where the file
  would change what happens, so a key repeating a value you already passed, or the one gdmutant
  would have used anyway, goes through in silence.
