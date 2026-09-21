---
type: decision
status: active
created: 2026-09-20
---

# Markers: find which test files reach each mutation spot, then skip or narrow the runs

## Status
Accepted as a design, to be built in the phases under [Plan](#plan). Nothing here is built yet.
Every phase is opt-in until the evidence listed under
[Evidence the implementation PRs must bring](#evidence-the-implementation-prs-must-bring) is in.

## Context
gdmutant runs the whole test suite once per mutant, in a fresh Godot process. That is simple and
always correct, and it is slow. [`DESIGN.md`](../design/DESIGN.md) NF-6 names the one speedup it
still defers: coverage-gated selection, meaning only run the tests that reach the mutated line.
`AGENTS.md` lists it as a non-goal for the first version, a scope choice for shipping that
version, and FG-4.1 folds "no test reaches this mutant" into survived until coverage data
exists. This ADR designs that deferred lever. It does not change the rule that each mutant gets
its own Godot process.

### What a real run spends its time on
Measured on a private game project: one source file, 20 mutants, 33 test suites, run through a
hand-written test harness with `--runner command`.

- Per mutant, about 14% of the time is Godot starting up, about 25% is loading test scripts, and
  about 61% is running tests.
- A typical mutation spot is reached by about 19% to 21% of the suites.
- None of the 20 mutants sat on a line that no test reached.
- gdmutant's own engine is about 0.4% to 3% of a real run.
- Timeouts: zero across 91 mutants in three projects, so tuning them is not being pursued.

So most of the time goes to suites that cannot possibly notice the mutant. But that harness
lists every suite as a `preload` in one constant array, so all 33 suites are loaded when the
harness script compiles, whatever it then runs. Its filter could skip running a suite, not
loading it. On that project the 25% load share stays fixed, and running only a fifth of the
suites would cut a mutant's cost to about 14% + 25% + 20% x 61%, roughly 51%, about 2x faster.
For a runner whose loading does shrink with the files it is given, the same arithmetic gives
14% + 20% x 86%, roughly 31%, about 3x. That 3x is the upper bound, not the expected figure.

The "no coverage" half gains nothing on that project, because every mutant was reached. It is
still worth having, because it is free once the map exists and it turns a misleading "survived"
into an honest "no test runs this".

### Step 0: what file subsets actually cost on GdUnit4 and GUT
Because the gain above depends on the runner, it was measured directly before any marker code,
with no markers at all: the whole suite against random subsets of about 20% of the test files,
passed on the command line. Godot 4.7, one machine, on copies of two real checkouts: GdUnit4's
own repository (132 test files, about 1,600 tests, run with `-a` once per file and `-c`) and
GUT's own repository at 9.7.1 (70 test files under `test/unit`, about 1,500 tests, run with
`-gtest=`). Four random subsets per framework, each run twice, and the whole suite two or three
times. Repeats agreed within a few percent.

| Framework | Whole suite | 20% subsets (four seeds) | Speedup per subset | Mean speedup |
|---|---|---|---|---|
| GdUnit4 | 139 s (135 to 143) | 9.4 s, 36.6 s, 10.8 s, 33.2 s | 14.6x, 3.8x, 12.7x, 4.1x | 6.1x |
| GUT | 126 s (126, 126) | 75.4 s, 27.6 s, 44.6 s, 39.4 s | 1.7x, 4.5x, 2.8x, 3.2x | 2.7x |

How the time splits, using the JUnit report's own per-suite times as "running tests":

- Starting Godot and the framework with a single small test file took 1.3 to 2.1 s.
- Everything outside the suites' own time (startup, discovery, loading) was about 5 to 7 s for
  the whole suite and about 2.5 to 3 s for a subset. So loading does shrink with the files given,
  unlike the preloading harness above, and it is only about 4% of a whole run here.
- The rest, about 96%, is running tests, and it is spread very unevenly: a few files that wait
  on timers and frames hold most of it. That is why the speedup swings from 1.7x to 14.6x with
  which files happen to be picked. A real map picks the files that reach a spot, not random ones,
  so a project's own numbers will differ, and the implementation PR must measure on one.

Both frameworks ran the files in exactly the order given, checked on every subset against the
framework's own output and its JUnit report, in forward and reversed order. The reverse marker
pass relies on this. (One GdUnit4 path in a subset was a base class with no tests, and was
correctly not run as a suite.) GdUnit4's own suite has a few failing tests on this machine, which
changes nothing about timing.

### What a marker is
A marker is one small call inserted before each mutation spot in a throwaway copy of the project,
which records "spot 17 was reached while this test file was running". Run the suite once on that
copy and you get a map from each spot to the test files that reach it. Then:

- A mutant on a spot that nothing reached is "no coverage", decided without starting Godot.
- Every other mutant runs only the test files that reach its spot.

Why this is sound, and where it stops being sound: a mutant changes one spot. Until that spot
runs, the mutated program does exactly what the original did. So a test that never reaches the
spot passes under the mutant exactly as it passed on the original, and cannot kill it. That
argument needs three things to hold, and each is a way the map can be wrong.

1. The test run is deterministic. A flaky test can reach a spot on one run and not the next.
2. The map credits every test that reaches the spot. A missing credit is the dangerous
   direction: the one test that could kill the mutant is not run, and the mutant reads as
   survived. An extra credit only costs time.
3. Tests do not depend on each other. If test file B only fails because file A ran first and
   left shared state behind (a static variable, an autoload), then running fewer files can
   change a verdict in either direction.

### What the real-Godot probes found
Probes against Godot 4.7, GdUnit4 6.1.3 and GUT 9.7.1, on a copy of this repo's `corpus/`:

- A marker on the same line as the statement, `_gdm_hit(N); <statement>`, parses and keeps every
  error's line number for `if`, `while`, `for`, `return`, assignment, `var` declaration and
  expression statements. `elif` rejects it with a parse error.
- One launch gives per-test attribution in both frameworks. GdUnit4's `before_test()` can read
  `__active_test_case`, and GUT's `before_each()` can read
  `gut.get_current_test_object().name`, and both can write it into an autoload (a node Godot
  creates before anything else runs) that holds the "current test".
- Code that runs at load time (class variable initializers, `_init`, `_ready`, other autoloads)
  runs before the first hook, so a sentinel "no test yet" value identifies it.
- Deferred code: an awaited timer is credited to the right test. An unawaited timer, and in GUT
  even `call_deferred`, fired during a later, unrelated test, sometimes in another file, and once
  after the final summary, and was credited to whichever test happened to be running. GdUnit4
  appeared to flush `call_deferred` inside the test, GUT did not. Neither framework reports it.
- A runtime SCRIPT ERROR aborts only the function it happens in, and the caller carries on. So a
  marker run that hit one can make covered code look unreached.
- `print` works but is buffered until exit and interleaves with the frameworks' coloured
  output. Collecting hits in the autoload and writing one JSON file when it is freed at exit was
  reliable, caught late hits, and avoids the Windows `cp1252` console trap.
- Cost: no measurable overhead for 8 markers on the corpus. A printing marker inside a hot loop
  made one run 7x slower.

Both frameworks also have a global event channel, which the probes did not use but which is the
better hook (see [Attribution](#attribution-by-test-file-with-file-windows)). GUT's `gut` object
emits `start_script`, `end_script`, `start_test` and `end_test`. GdUnit4 has
`GdUnitSignals.instance().gdunit_event`, whose events include `TESTSUITE_BEFORE`,
`TESTSUITE_AFTER` and `TESTCASE_BEFORE`, and a session-hook service.

### Spike: can Godot's debugger report executed code without editing the source?
Checked in headless Godot 4.7, as the alternative to markers.

- With no debugger attached (a plain `--headless` run), `EngineDebugger.is_active()` is false.
  `register_profiler` and `register_message_capture` succeed, but the profiler is never switched
  on and never ticks, `send_message` fails with "No active debugger", and none of Godot's own
  profilers exist. The process also crashed with a segmentation fault at exit after registering
  a profiler. So nothing is available.
- With `--remote-debug` pointed at a local listener, the debugger is active and a custom profiler
  is switched on and ticks every frame. Its callback receives only frame timings: no function, no
  line, nothing about which code ran. Godot's built-in profilers registered there are `servers`,
  `visual` and `performance`. The editor's script-function view is fed from these over Godot's
  binary debugger protocol, so using it would mean gdmutant acting as a debugger server, and it
  reports functions with timings per frame, not which statements ran or which test ran them.

So the debugger cannot say which spots ran without editing the source, headless or not. It is
rejected below.

### Prior art, and what we take from each
The decision here is to adopt the one idea that matters from each tool rather than depend on the
tool. Each of these mostly comes down to one trick, which is cheaper to own than a dependency is
to track.

| Source | What we take | Why not the tool itself |
|---|---|---|
| [Stryker](https://stryker-mutator.io/docs/stryker-js/configuration/) `coverageAnalysis` off / all / perTest | The three levels: none, "no coverage" only, per-unit selection. The static-mutant rule: a mutant in code that runs at load time runs every test ([static mutants](https://stryker-mutator.io/docs/mutation-testing-elements/static-mutants/): 6% of mutants were 50% of the runtime). NoCoverage scoring. | Stryker instruments JavaScript. It cannot read GDScript, so there is nothing to depend on. We do not take `ignoreStatic`, which skips those mutants, because it once mis-scored them ([stryker-js#3774](https://github.com/stryker-mutator/stryker-js/issues/3774)). |
| [Infection](https://github.com/infection/infection/issues/1825) | Its failure, turned into a standing check. A PHPUnit upgrade silently broke its per-test map and the kill rate fell from 90% to 42%, with nothing saying so. We run a sample of mutants both ways on every run and fail on any disagreement. | PHP only. |
| [PIT FAQ](https://pitest.org/faq/) | Its warnings list: code run once at class load, and hidden test-order dependence, both make per-test selection report wrongly. They shape the load-time rule and the order check below. | Java only. |
| [mutmut 3](https://github.com/boxed/mutmut/blob/main/ARCHITECTURE.rst) | Recording which tests reach which code in the same pass as a normal test run. | Python only, and it records by function. gdmutant knows each mutant's exact statement, so it can record by statement. |
| [cargo-mutants](https://mutants.rs/vs-coverage.html) | Its caution. It deliberately does not use coverage, preferring always-correct to fast. So selection stays opt-in until the evidence is in, and "off" never goes away. | Rust only. |
| [Nano Coverage](https://github.com/IgorBayerl/nano-coverage-godot) | Its output shape: accumulate hits in an autoload and write them once at exit. Also confirmation that a GdUnit4 session hook is a workable place to plug in. | Alpha, with no prebuilt binaries. It is a native GDExtension, so each platform and Godot version needs a compiled build. Besides a framework-agnostic standalone mode, GdUnit4 is its only test-framework integration so far, with GUT on its roadmap. Its README describes line coverage, not which test reached a line. Depending on it would tie every gdmutant user to its release cycle for something a few lines of GDScript in a throwaway copy can do. |
| [GdUnit4 CLI](https://godot-gdunit-labs.github.io/gdUnit4/latest/advanced_testing/cmd/) `-c` / `--continue` | Pass it on the marker run. By default GdUnit4 stops at the first failure, which would hide how much of the suite actually ran. | Not a new dependency. GdUnit4 is already the user's framework. |

One case where depending on something is clearly better: the frameworks' own event channels for
"a test file started" and "a test file ended". The alternative is injecting a `before_test` or
`before_each` into every suite of the copy, which collides with any suite that already defines
one and has to be merged into user code. The frameworks already announce these moments, gdmutant
already depends on the framework, and a missing event is detectable (see
[Keeping the map honest](#keeping-the-map-honest)). So the hook uses the framework's API, and
injecting into suites is only the fallback if that API turns out not to fire in a CLI run.

## Decision

### Adopt the ideas, not the tools
gdmutant builds its own markers and takes one idea from each tool in the prior-art table, for the
reasons given there: none of them runs on GDScript except Nano Coverage, which is alpha, native,
per-platform and GdUnit4-only, and each idea is a few lines to own. The one dependency this
design adds is on the test frameworks' own "file started" and "file ended" events, which is
better than the alternative and adds no new package, since the framework is already there.

### Where markers go
Markers exist only in a throwaway copy of the project, made the way `--jobs` already makes its
worker copies. Mutant runs never see them. Only files that have runnable mutants get markers.

A spot is the statement that holds a mutant. The rule for every spot: its marker must run every
time the mutated code runs, at the same moment or just before it, inside the same function call.
Running more often is fine (an extra credit). Running less often, or at a different time, is not.

| Where the mutant is | Marker |
|---|---|
| A statement in a function body: `if`, `while`, `for`, `match`, `return`, assignment, `var`, expression statement | `_GdmMarks.hit(N); ` inserted at the start of the statement's first line, so every line number stays the same. |
| An `elif` condition | The marker of the `if` that starts the chain. An `elif` condition is evaluated whenever the conditions above it are false, even when its body never runs, so a marker inside the `elif` body (the probes' workaround for the parse error) would miss exactly the tests that could kill a condition mutant. |
| A statement inside an `elif` or `else` body | Its own marker, as for any statement. |
| A `match` pattern | The marker of the `match` statement. |
| A default parameter value | A marker as the first statement of the function body. It runs on every call, a superset of the calls that use the default. |
| A body written on the header's own line, such as `if x: return y` | The marker of the header statement. |
| A multi-line lambda body | Markers inside the lambda's own statements. The statement that creates the lambda is not enough, because the lambda can be called later, from another test. |
| A single-line lambda, a statement containing `await` | Run everything. An `await` splits the statement across a pause, so the part after it can run in a later test than the marker. |
| A class-level `var` initializer (including `static`, `@onready`, `@export`), a `const`, an `enum` value, an annotation's argument, a property's type or default, a signal declaration | No marker is possible. Run everything. |

Run everything means what gdmutant does today: the whole suite. A spot that cannot take a marker
is never "no coverage".

The marker is a single call, so it can prefix a statement without changing its meaning.
`_GdmMarks` is an autoload the copy's `project.godot` gains, registered first so it exists before
any other autoload runs. If the project already uses that name, the marker run fails with a clear
message. The call records a hit only when the spot's last recorded window differs from the
current one: one comparison per call, so a hot loop records once and pays little. It records per
window, not per spot, because recording only a spot's first hit ever would drop every later test
file that reaches it, which is the dangerous direction. The implementation must measure the
hot-loop cost, since the 7x slowdown came from a printing marker, not this design.

### Attribution by test file, with file windows
The unit is the test file (a suite), not the test case.

- It is what both frameworks can run. GdUnit4's `-a` takes suite paths and repeats. GUT's
  `-gtest` takes a list of test script paths. Both replace today's single test directory.
- It removes most of the deferred-code problem for free. Deferred code that fires in a later test
  of the same file is credited to the same file, which runs as a whole.
- The measured split limits what finer units could add. With per-file selection the startup and
  loading of the selected files remain. Per-test selection could at best trim part of the 61%
  spent running tests inside those files, about 1.6x more at the very most, against a more
  complex map, a different command per mutant, and per-test filtering that is a name-substring
  match in GUT. Infection's lesson is that the finer and more clever the map, the more ways it
  breaks quietly. Revisit only with a measured run showing the files themselves are the cost.

A window opens when a test file starts (GdUnit4 `TESTSUITE_BEFORE`, GUT `start_script`) and
closes when it ends (`TESTSUITE_AFTER`, `end_script`). Suite-level setup such as GdUnit4's
`before()` or GUT's `before_all()` falls inside its file's window, which a per-test window would
have missed. Test-case names may be recorded too, for diagnostics only.

### Load-time and out-of-window hits run everything
A hit while no window is open means code that ran at load time, between files, or after the last
file (a late timer, or deferred work after the summary). The spot is flagged run everything.
This is Stryker's static-mutant rule. There is deliberately no option to skip these mutants.

Deferred code that crosses into a later file's window is not caught by the window alone: it is
credited to the later file, and the file that started it is missing. The second marker pass
below catches it by running the files in reverse order. In the forward order the late hit lands
in a file after the one that started it. In reverse it lands in a file that was before it, or
outside every window. Those can never be the same file, so the spot's file set differs between
the two passes and the spot is flagged run everything.

### The marker run must be clean
The marker run is two passes of the whole suite on the marked copy: forward order, then reverse
file order. Each pass must meet all of these, or the run stops with an error that says which one
failed and suggests turning markers off:

- Every test passes. GdUnit4 runs with `-c`, so a failure does not hide how far the suite got.
- The output contains no `SCRIPT ERROR`, anywhere, including load time and after the summary. Both
  frameworks already fail a test on a script error inside it by default, but not outside a test,
  and an error elsewhere can make covered code look unreached.
- The hits file exists and parses. A crash at exit that loses it must not read as "nothing
  reached anything".
- At least one hit was recorded, and at least one window opened. For the JUnit runners, every
  test file in the JUnit report opened a window. A hook that never fires would otherwise turn
  every spot into run everything or no coverage without a word.
- The test count equals the baseline's.

One exception is not an error. If the forward pass is clean but the reverse pass has failing
tests, the suite depends on file order. Selection is then refused for the run, with a warning
naming the files that failed, and the run continues with "no coverage" only, which needs only
the forward pass to be sound.

The map is the union of both passes. A spot whose file set differs between the passes is flagged
run everything and counted in the run's summary as order-dependent. So the second pass is the
flakiness check and the order check in one.

### Keeping the map honest
- The self-check. On every run with selection on, a small deterministic sample of mutants
  (a few per run, chosen by a stable hash so runs repeat, and always including one "no coverage"
  mutant when there is any) is also run against the whole suite. Killed and timeout count as the
  same answer. Any other disagreement fails the run with both verdicts shown. The run always
  states how many mutants the self-check compared, so an empty sample is visible, never a silent
  pass. A flag runs the self-check on every mutant. That is the two-sided equivalence check below,
  kept as a feature. The sample is a tripwire, not proof. It exists because Infection's map broke
  quietly on a framework upgrade, and a broken hook is exactly what a green selected run cannot
  show.
- Kills under selection are confirmed. Running fewer files can make a test fail that normally
  passes, because it relied on state left by a file that was not selected. That would read as a
  kill the mutant did not cause. So the first time a given set of files kills a mutant, the same
  set is run once on the unmutated copy (cached per set). If it fails there, the kill is not
  trusted: the mutant is re-run on the whole suite, and the set is reported as order-coupled,
  separately from real kills.
- Flaky tests. Selection does not fix flakiness, which already swings mutation scores by 5 to 10
  points (Shi, Bell and Marinov, ISSTA 2019). The two-pass union and the self-check stop flakiness
  from quietly shrinking the map. A flaky baseline is still the user's problem to fix first.

### The command runner
An exit code cannot say which tests ran, so `--runner command` cannot select without the
harness's help. "No coverage" needs no help: any hit anywhere during a clean marker run means
covered. Selection needs the harness to opt in to a small contract, and gdmutant never assumes it
did.

- Windows. In the marked copy the harness calls `_GdmMarks.begin_file(path)` and
  `_GdmMarks.end_file(path)` around each test file, guarded by a check that the autoload exists,
  so the same harness is untouched in normal runs. If no window opens during the marker run,
  selection is off for the run, said out loud, and "no coverage" still applies.
- Selection. gdmutant sets `GDMUTANT_SELECTED_TESTS` to the test file paths to run, one per line,
  and `GDMUTANT_SELECTION_RECEIPT` to a file path.
- The receipt. A harness that honoured the selection writes the paths it actually ran to the
  receipt file. No receipt means the harness ran everything. That is the reference behaviour, so
  the verdict stands, with one warning per run that selection was ignored. A receipt listing every
  selected path, and possibly more, is trusted. A receipt missing a selected path is an `error`
  verdict. The receipt file is deleted before each run, the same freshness rule the JUnit runners
  use for their reports.

The private project that motivated this uses such a harness, so it gains nothing from selection
until its harness opts in.

### Selection keeps every existing check
A selected run is a second path to a verdict, beside the whole-suite run, so it must check
everything the whole-suite path checks. GdUnit4 still raises on a zero-test report. GUT's
drop-below-baseline guard (ADR-0011) compares against the sum of the selected files' test counts
from the marker run, not the whole suite's count, or every selected run would look like a
dropped suite. The engine's zero-test backstop is unchanged. The per-mutant timeout stays derived
from the whole-suite baseline, which is generous for a smaller run and never too tight.

### The NoCoverage verdict
A new verdict, `no coverage`, for a mutant whose spot no test reached in either marker pass. It
maps to `NoCoverage` in the mutation-testing-elements JSON schema, which already defines it.

It is scored the way Stryker scores it: as undetected, in the denominator, so
score = detected / (detected + survived + no coverage). A score does not jump when markers are
switched on. The console, the JSON report, the HTML report and the survivor explanations list
"no coverage" mutants beside survivors under their own label, since "nothing runs this line" and
"something runs it but checks nothing" call for different fixes. Anything that today acts on
survivors, such as the exit code and thresholds, acts on these too. These are several paths that
must agree, and the implementation PR says what each one does.

### How it is switched on
One option, `--coverage-analysis`, with `off` (today's behaviour, the default), `all` ("no
coverage" only) and `per-file` (selection too). The names follow Stryker's so they are familiar.
The default changes only by a later decision, backed by the evidence below from more than one
real project.

### Where the code lives
The GDScript adapter owns placement (the statement-kind table above) and the marker autoload. The
engine owns the map, the verdict, the selection decision and the self-check, all
language-neutral: it sees spot numbers, file paths and "run everything" flags, never GDScript.
Each runner owns how it passes a file list (`-a` per file, `-gtest=`, the environment variable)
and the window hooks, through optional protocols like `Preparable`, so a runner that cannot
select simply does not implement them.

### Relationship to other decisions
- It reverses the first version's non-goal on coverage-gated selection. The first implementation
  PR removes it from `AGENTS.md`'s non-goals and updates NF-6, FG-4.1 and §5 of `DESIGN.md`.
- One Godot process per mutant stays (ADR-0011's runner contract). Keeping one Godot alive for
  every mutant is out of scope, in part because static variables are not reset on hot reload
  (Godot issue 105667).
- ADR-0015's `SCRIPT ERROR` scan, today only in the command runner, applies to the marker run for
  every runner.
- ADR-0005's exit-code contract gains the optional selection contract above and is otherwise
  unchanged.
- ADR-0016 names this very change as its revisit trigger: selection shrinks Godot's share of each
  mutant. The implementation PR must re-measure the engine's share. At 2x to 3x less Godot time,
  0.4% to 3% becomes roughly 1% to 9%, under 0016's 10% threshold. At the 6x seen on GdUnit4's own
  suite, 3% becomes about 16% by the same arithmetic, which would cross it.

## Alternatives considered
- Godot's debugger or profiler instead of source markers. Rejected by the spike above: nothing
  with no debugger attached, and only frame timings or per-frame function timings over a binary
  protocol with one attached.
- Nano Coverage. Rejected as a dependency for the reasons in the prior-art table. Its
  accumulate-and-flush shape is adopted.
- Per-test-case selection. Deferred, see the attribution section.
- Injecting a `before_test` or `before_each` into every suite. Kept only as the fallback if the
  frameworks' event channels do not fire in a CLI run.
- Printing hits to stdout. Rejected: buffered, interleaved with framework output, and exposed to
  the Windows console code page.
- Skipping load-time mutants (Stryker's `ignoreStatic`). Rejected: it hides mutants, and Stryker's
  own version once mis-scored them.
- One marker pass instead of two. Rejected: it cannot see flakiness or deferred code that crosses
  files, and the second pass costs one suite run, trivial next to the mutants.

## Plan
Each step is its own PR, off `main`, in this order.

0. A go or no-go gate on steps 3 and 4 only, measured before either is built: is a run on about
   20% of the test files at least about 2x faster than the whole suite, averaged over several
   random subsets with repeats? Steps 1 and 2 do not depend on it. An honest "no coverage"
   verdict is worth having whatever the speed.
   - GdUnit4 (`-a` per file): met, 6.1x mean, worst subset 3.8x. See
     [Step 0](#step-0-what-file-subsets-actually-cost-on-gdunit4-and-gut).
   - GUT (`-gtest=`): met, 2.7x mean. One subset of four came in at 1.7x, because it happened
     to hold the slowest files. So step 3 goes ahead.
   - Command runner: not measurable in general, since the gain depends on the harness. The one
     measured harness preloads every suite, which caps it at about 2x, right at the bar. So step 4
     waits until a harness that loads only the files it is told to run has been measured against
     the same bar. Until then, command-runner users get "no coverage" from step 2 and nothing more.
1. Marker placement, adapter only. Turn a source file into a marked source file and a spot table,
   following the placement table, with no engine change. Tested against real Godot: every marked
   corpus file parses, keeps its error line numbers, and every spot that can take a marker fires.
2. The marker run and "no coverage", for all three runners (`--coverage-analysis all`). The
   autoload, the JSON written at exit, the two passes, every clean-run check, and the new verdict
   through console, JSON, HTML and scoring. Updates `AGENTS.md` and `DESIGN.md`.
3. Selection for GdUnit4 and GUT (`--coverage-analysis per-file`). The file-window hooks, the
   file lists per runner, GUT's per-selection drop guard, the confirmation of kills, and the
   self-check.
4. The command-runner contract: windows, the environment variables and the receipt. Only once
   step 0's bar is met for a command harness.
5. A decision on the default, in its own PR, only after steps 2 to 4 have run on real projects.

## Evidence the implementation PRs must bring
1. A two-sided equivalence check: the same verdict for every mutant with and without selection,
   on the corpus and on at least one real project. It must include mutants killed only by a suite
   that a wrong map would drop, built on purpose if the real projects have none, so a check that
   would pass with a broken map is impossible. Run it with the self-check on every mutant.
2. The other side: a deliberately broken map (a hook that never fires, a dropped credit) must make
   the self-check and the clean-run checks fail. A self-check that has never been seen to fail
   proves nothing.
3. Real-Godot tests, on both JUnit frameworks, for: load-time code (class initializers, `_init`,
   `_ready`, autoloads) flagged run everything, an unawaited timer and `call_deferred` crossing
   into another file flagged by the reverse pass, a hit after the summary, a SCRIPT ERROR outside
   any test failing the marker run, and an order-dependent suite refusing selection.
4. Placement tests covering every row of the placement table, including an `elif` condition
   mutant that is killed only by a test whose path never enters the `elif` body.
5. Before and after wall-clock times on a real project, and the share of spots flagged run
   everything (Stryker saw a small share of such mutants take half the time).
6. The hot-loop cost of the marker call, measured.
7. New pure logic mutation-tested with gdmutant's own self-mutation setup
   (`docs/mutation-testing.md`), survivors driven to zero or justified.

## Consequences
- A run with markers on costs two extra suite runs up front, plus a few whole-suite runs for the
  self-check and confirmations. In exchange, per-mutant time drops by a factor that depends on
  the runner and on how test time is spread across files: about 2x at most for a harness that
  preloads every suite, and a mean of 2.7x to 6.1x for random fifths of GUT's and GdUnit4's own
  suites.
- A second path to every verdict now exists. The self-check, the confirmation of kills and the
  clean-run checks are what keep the two paths agreeing, and they are part of the feature, not
  extras.
- Order-dependent and flaky suites get less speedup, never a wrong answer they are not told
  about.
- Command-runner users get "no coverage" for free and selection only by changing their harness.
- The mutation score is unchanged by switching markers on, except where a mutant that used to be
  "survived" becomes "no coverage", which scores the same.
