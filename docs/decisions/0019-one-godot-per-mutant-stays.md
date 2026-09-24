---
type: decision
status: active
created: 2026-09-24
---

# One Godot process per mutant stays, and here is what would change that

## Status
Accepted. Nothing in the tool changes. gdmutant keeps starting a fresh `godot --headless` for every
mutant. What follows is the spike that settles it, the seams it found, the admission test a future
implementation would need, and the three numbers that would reopen the question.

## Context
[The design](../design/DESIGN.md) calls keeping one Godot alive across mutants "the remaining lever"
and rules it out in a single line, citing Godot issue 105667, that static variables are not reset on
hot reload. Three of the four speedups it names have shipped. This one was never measured, only
cited, and the citation is about one mechanism rather than about the whole idea.

Two other things made it worth measuring now.
[ADR-0018](0018-coverage-analysis-stays-off-by-default.md) reports 13.7 s for a mutant that runs only
the test files reaching it, against 103.3 s for a whole-suite one, and the theoretical floor for
running a handful of pure-logic tests is tens of milliseconds. So there is a large gap left, and the
obvious suspect is the process. ADR-0018 also observed, on a small project, that "most of a mutant's
cost there is Godot starting, not tests running".

The question this ADR settles is not how to scrub Godot's state between mutants. It is how much
reuse can be had while a verdict stays exactly as trustworthy as a fresh process makes it, and how
that trust would be proved.

### How this was measured
One machine, Windows 11, Godot 4.7-stable, gdmutant at `480c72c`. Every project was copied first and
measured in the copy. Every Godot invocation was `--headless` and named a script or a framework's own
command-line entry. A bare `--headless --path <project>` is not a usable measurement: the project has
no main scene, so Godot raises a dialog, and the first round of numbers taken that way was thrown out
and retaken.

Three fixtures, chosen so the answer does not rest on one suite's shape:

| Fixture | Runner | Suites | Tests | One fresh run |
|---|---|---|---|---|
| The bundled corpus | GdUnit4 | 2 | 5 | 1.455 s |
| 100 copies of the corpus suite | GdUnit4 | 100 | 300 | 6.158 s |
| GUT's own repository, `test/unit` | GUT | 60 files | 1,524 | 124.5 s |

The GUT repository was also measured with a single test file selected, which is what a mutant run
looks like under `--coverage-analysis per-file`: 2.285 s.

## What was measured

### A fresh process is mostly not the process
A headless Godot that boots, runs a `SceneTree` script which quits at once, and exits, takes 0.161 s
on the corpus and 0.156 s on the 60-file project. That is the whole cost of the fresh process itself,
and it does not grow with the project.

Set that beside what a mutant actually costs:

| Run | Total | Engine boot's share |
|---|---|---|
| Corpus, GdUnit4, whole suite | 1.455 s | 11% |
| 100 suites, GdUnit4, whole suite | 6.158 s | 2.6% |
| 60-file GUT suite, one file selected | 2.285 s | 6.8% |

GdUnit4 reports 32 ms of test time for the corpus run that takes 1.455 s. So on that fixture 89% of a
mutant is neither the process nor the tests. It is the framework loading, compiling and discovering
inside the process. That is the part a warm process can skip, and it is much larger than the part
everyone assumes is the target.

### Both frameworks can run twice in one process, and both fight it
`GdUnitCmdTool.gd` and `gut_cmdln.gd` are both `extends SceneTree`. A `SceneTree` is the main loop,
so neither can be instantiated twice. What can be reused is the node each one wraps:
`GdUnitTestCIRunner` for GdUnit4, `GutRunner` for GUT.

Both end a run with `get_tree().quit()`, and that call cannot be intercepted from the tree side. A
script that overrides `SceneTree.quit()` is refused outright by the GDScript parser, and when that
warning is silenced the override still never fires, because a GDScript call through a statically
typed `SceneTree` reference goes straight to the native method. A probe that overrides `quit()` and
has a child node call `get_tree().quit(7)` exits with code 7 and never reaches the override.

GUT's exit is unconditional in headless mode. `GutRunner._handle_quit` computes
`should_exit or should_exit_on_success_and_green or GutUtils.is_headless()`, so dropping `-gexit`
changes nothing.

Each framework has exactly one seam that works, and neither is documented:

- GdUnit4 emits a session-close event at the end of its runner's `RUN` branch, after the suite has
  finished and the report has been written, one frame before the branch that quits. Turning the
  runner's processing off on that event keeps the process alive. Separately, GdUnit4's argument
  parser discards every argument before the one containing its own tool name, so a differently named
  driver script sees no command line at all. The runner carries a `_debug_cmd_args` field that
  replaces it, which is also what would let each mutant get its own test selection.
- GUT's `GutRunner.quit()` is an ordinary GDScript method, so a subclass can take it over. Swapping
  the scene's script for that subclass before the node enters the tree is the whole trick. One
  further quirk: on the first, cold instantiation the runner's `@onready` references to its own GUI
  nodes are still null immediately after `add_child`, and calling `run_tests` there dies with
  "Nonexistent function 'add_child' in base 'Nil'". One frame of waiting fixes it, and on every later
  instantiation the references are already set.

With those in place, both run repeatedly. Twenty consecutive GdUnit4 passes on the corpus all
reported 5 tests, 0 failures, and each wrote a fresh report after the previous one had been deleted.
Five consecutive GUT passes on the 60-file project all reported 14 tests and 0 failures.

### What a warm process is worth
Steady-state cost per pass, against one fresh process doing the same work:

| Fixture | Fresh | Warm, first pass | Warm, steady | Ratio |
|---|---|---|---|---|
| Corpus, GdUnit4, whole suite | 1.455 s | 0.44 s | 0.064 s, flat over 20 passes | 22.7x |
| 100 suites, GdUnit4, whole suite | 6.158 s | 5.485 s | 3.19 s | 1.9x |
| 60-file GUT suite, one file | 2.285 s | 1.86 s | 1.61 s | 1.42x |

The 22.7x is the number to distrust. That fixture's tests take 32 ms, so almost everything in it is
cacheable framework startup, and it flatters reuse by exactly as much as it is unrepresentative.
The two fixtures with real work in them say 1.9x and 1.42x. The saving in each case is much larger
than the 0.16 s of process launch, which confirms that what reuse buys is cached script compilation
and discovery rather than the process.

There is a small upward drift in the 100-suite warm passes, from 3.186 s to 3.249 s over five, which
is worth watching but is not a per-mutant tax anyone would notice.

### Without an explicit reload, every mutant survives
Five real mutants of the corpus module, each one first in a fresh process and then in a warm one:

| Mutant | Fresh | Warm, file rewritten only | Warm, with the script reloaded |
|---|---|---|---|
| `>` to `>=` | killed, 1 failure | survived | killed, 1 failure |
| `<` to `<=` | survived | survived | survived |
| `and` to `or` | survived | survived | survived |
| `true` to `false` | survived | survived | survived |
| `== 1` to `!= 1` | killed, 4 failures | survived | killed, 4 failures |

Rewriting the file and running again is not enough. The compiled script is already in the process, so
the suite runs the original code and reports it green. Every mutant comes back survived, the report
is fresh, the test count is right, and nothing anywhere says the mutant was never loaded. That is the
exact shape [`AGENTS.md`](../../AGENTS.md) calls recurring bug one, a gate that passes without
checking anything, and here it corrupts a score silently and completely.

Three ways of forcing a reload were tried, and all three fix it, with verdicts and failure counts
matching the fresh process exactly: loading through the resource loader with the deep
ignore-the-cache mode, feeding the cached script its new source text and reloading it, and doing that
to the test scripts as well. The second is the one to prefer, because it does not depend on the
cache mode, which has open bugs of its own (Godot issue 59669).

So source staleness is real, and it is solved. It is not the blocker.

### What leaks, and it is not the source
The blocker is state. The same unmutated suite, run six times over, with nothing mutated at all. A
fresh process gives the same answer all six times: 4 tests, 0 failures. A warm process does not.

| Pass | Static counter | Failures | Which tests failed |
|---|---|---|---|
| 1 | 1 | 0 | |
| 2 | 2 | 1 | the autoload one |
| 3 | 3 | 3 | the autoload, the static counter, the leftover node |
| 4 | 4 | 3 | the same three |
| 5 | 5 | 3 | the same three |
| 6 | 6 | 3 | the same three |

The fixture is four ordinary tests. One calls a static function. One asserts that a class's static
counter is still small. One asserts that an autoload's field is still zero, then writes to it. One
adds a node to the root and asserts that few such nodes exist. Every one of them passes in a fresh
process, every time, however many mutants have run before.

Three separate things leak, and each one flips a verdict from survived to killed:

- The static variable climbs by one per pass, from 1 to 6. Reloading the script with the new source
  does not reset it, which confirms Godot issue 105667 first-hand rather than by citation.
- The autoload is built once when the process starts, so the second pass sees the first pass's write.
- Nodes a test leaves in the tree are still there for the next pass.

None of this is fixed by any reload. The reload fixes the code. Nothing available from GDScript
fixes the state, because the state lives in the engine's object database, the global class cache and
the scene tree, all of which outlive any script.

The failure direction matters. Leaked state makes tests fail, a failing test means killed, and killed
counts as detected. So a leaky reused process reports a better mutation score than the truth, and
reports it without an error anywhere in the run.

### A mutant Godot refuses to compile
Three passes, healthy then broken then healthy, where broken means source gdtoolkit would accept but
Godot rejects at load.

A fresh process behaves as [ADR-0011](0011-runner-agnostic-adapter-seam.md) describes: GdUnit4 aborts
at discovery, exits 105, and writes no report, which the engine tallies as `error` and leaves out of
the score.

A warm process does something different. The reload of the broken source returns a failure and the
script reports that it cannot be instantiated, the run then goes ahead, and GdUnit4 writes a report
with three errors in it. The engine would read that as `killed`, which counts as detected.

So the two roads give a different verdict for the same mutant, and the warm one moves the score the
wrong way again. The good news, such as it is: the warm process recovers completely. The third pass
is byte-identical to the first. A broken mutant does not poison the ones after it.

### The driver's own exit code is not usable
On Windows, a driver that bypasses a framework's own quit path exits with an access violation every
time, including with a single pass and with every variation of when the runner is freed. Both
frameworks' cleanup lives in the code path being skipped. All the reports were written and correct
before it happened, so a resident driver would have to take its answers from the report file alone
and treat its own exit code as noise. gdmutant already reads the report, but it also raises when a
run writes none, and that error would now have two quite different causes.

## Decision
gdmutant keeps starting one `godot --headless` per mutant.

The reason is not that reuse cannot work. It demonstrably runs, it recovers from a broken mutant, and
the source-staleness half is solved. The reason is the trade. A real project gets somewhere between
1.4x and 1.9x. Against that sit three costs.

The first is the failure mode. A reused process that leaks reports a higher score than the truth and
says nothing. gdmutant's own NF-5 puts a wrong mutant at the top of the list of things never to ship,
because a wrong mutant means a silently wrong survivor report. Leaked state is that same failure with
a different cause, and unlike an invalid mutant it is invisible at generation time.

The second is where the seams are. Nothing used above is a documented interface. It is a private
field on one framework's runner, an internal event on the same runner's state machine, a subclass
override of another framework's method, and a one-frame ordering quirk in a scene's readiness. Each
is a thing a minor version of either framework can change without noticing, and
[ADR-0011](0011-runner-agnostic-adapter-seam.md)'s whole point is that a new JUnit framework becomes
first-class by adding one small adapter. Reuse would give every adapter a second, much larger
obligation, and a framework without such a seam could not be supported at all.

The third is that `--jobs N` already exists, gives about 3x on four workers, needs no new trust, and
costs nothing to maintain. Reuse would have to beat that margin to be worth its risk, and at 1.4x to
1.9x it does not.

### If it is built later, this is the shape
The design question was how trust would be proved, and the spike answers it, so the answer is
recorded here rather than rediscovered.

The admission test is the one above, run on the user's own project before any mutant. Run the
unmutated baseline several times in one warm process and require every repetition to give the same
result as the first. A project that leaks fails on the second or third repetition, as the fixture
above does. A project that does not leak passes and is safe to reuse. This is exactly the shape
[ADR-0017](0017-markers-for-no-coverage-and-test-selection.md) already uses for test selection, where
a reverse marker pass measures whether the suite depends on the order its files run in and refuses to
select when it does. The cost is a handful of warm baseline runs, which are the cheap ones.

Around it:

- The mutated script is reloaded explicitly by feeding the cached script its new source text, never
  by rewriting the file alone, and never by trusting a cache mode.
- Reuse is bounded. One process runs at most N mutants and then exits, so any drift the admission
  test missed can travel only that far.
- Every Kth mutant is also run in a fresh process and the two verdicts must agree, which is the same
  self-check the coverage path already carries.
- A mutant whose coverage data shows its only marker hits at load time is never reused. It is the
  mutation-testing equivalent of a static mutant, the kind that cannot be re-evaluated in a process
  that has already loaded the code, and gdmutant already collects exactly the data needed to spot it.
- The verdict comes from the report file. The driver's exit code is not evidence of anything.
- It is off by default, for the reasons [ADR-0018](0018-coverage-analysis-stays-off-by-default.md)
  gives for the other lever that trades correctness risk for speed.

## Alternatives considered

### Make a mutant a value the program reads, rather than an edit to the code
Compile every mutant into the source once, each behind a check of a global "which mutant is active"
value, and switch that value per run. Then nothing is recompiled and nothing needs resetting, which
is the neat answer to the operator's constraint and is what one mature mutation tester does.

It does not help here. The spike shows that source staleness was already solvable and that the real
blocker is test state: an autoload's field, a class's static, a node left in the tree. Switching does
not touch any of those. It removes the problem that was solved and leaves the one that was not, while
adding a large new obligation, that the instrumented source must compile and behave identically to
the original under every framework. The same tool's own documentation says its static-initialiser
mutants have no effect, which is the same wall from the other side.

### Reuse only where nothing can leak
Detect up front that the files under test have no statics, the project has no autoloads and the tests
leave nothing in the tree, and refuse otherwise. Strictly weaker than the admission test above and
harder to get right: it has to enumerate every leak in advance, from source, in a dynamic language,
and the fixture above found three kinds in four tests. Measuring repeat-stability directly catches
every kind at once, including ones nobody thought of.

### Keep the process but give each mutant a fresh scene tree
There is no such thing to give. A `SceneTree` is the main loop, and Godot cannot be initialised a
second time in one process. Embedding the engine as a library landed in 4.6, but re-initialising it
within a process explicitly did not. Godot has no fork, and Windows has no fork at all, so the trick
some testers use on Linux is unavailable in both directions.

### Say the gap is unreachable
Close to the decision taken, but overstated. The gap is reachable. It is worth 1.4x to 1.9x, which is
real, and the mechanism works. What it is not worth is the trust it would cost at today's prices.

## Consequences
- The design's NF-6 line about this lever stands, but its reason was too narrow. Static variables are
  one of three things that leak, and the one an explicit reload most obviously fails to fix. The
  design should point here instead of at a single issue number.
- Two facts about Godot are now measured rather than assumed, and both are reusable. A headless engine
  boot is about 0.16 s and does not grow with the project, so any future "Godot startup is the cost"
  claim should be checked against that. And a framework's in-process load, compile and discovery is
  the real per-mutant fixed cost, which is what test selection under
  [ADR-0017](0017-markers-for-no-coverage-and-test-selection.md) does not shrink either.
- Anyone reopening this starts from the seams named above rather than from a blank page.

## When to revisit
Three things would change the answer, and the first is the one that matters.

1. A framework offering a documented in-process re-run, so the seam stops being private. That removes
   the second cost entirely and most of the first, since a supported path can be expected to reset
   what it owns.
2. Godot resetting statics on reload, or offering any way to clear an autoload and the tree to their
   process-start state. Godot issue 105667 is the one to watch. That would take the leak from three
   kinds to one and might make the admission test pass on ordinary projects instead of unusual ones.
3. A measured ratio above about 3x on a real project, which is where reuse would start to beat
   `--jobs` rather than merely add to it. The place to look is a large suite under
   `--coverage-analysis per-file`, where the tests a mutant runs are few but discovery is still paid
   in full. That combination was not measured here, and it is the one case where the fixed cost reuse
   removes is the dominant term.

## Evidence an implementation PR must bring
If this is ever built, the PR carries all of the following, on real Godot, not mocked.

- The admission test failing on a project built to leak, in at least the three ways above, and passing
  on one that does not. The failure has to be a refusal to reuse, never a run that continues.
- A whole run of a real project, every mutant's verdict compared one for one against the same run with
  reuse off, with zero disagreements. A score that merely matches is not enough, because two verdicts
  can move in opposite directions and leave the total where it was.
- The broken-mutant case pinned both ways: that a mutant Godot refuses to compile is tallied the same
  as it is without reuse, and that the mutant after it is unaffected.
- Timings from at least two projects of different shapes, including one whose tests cost more than its
  framework's startup, so the ratio is not quoted from a fixture like the corpus.
- The driver deriving its verdict from the report alone, with a test proving an abnormal driver exit
  does not change any verdict.
- A stated, tested behaviour for each framework version the seams were verified against, and a check
  that fails loudly when a framework upgrade moves one, rather than quietly falling back to a run that
  reuses nothing and says nothing.
