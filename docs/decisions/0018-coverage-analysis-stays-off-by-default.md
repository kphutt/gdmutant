---
type: decision
status: active
created: 2026-09-23
---

# Coverage analysis stays off by default

## Status
Accepted. This is step 5 of [ADR-0017](0017-markers-for-no-coverage-and-test-selection.md)'s plan,
the decision on what `--coverage-analysis` defaults to. The answer is `off`, which is what it
already is, so nothing in the tool changes. What follows is the measurement that settles it and the
trigger that would reopen it.

## Context
ADR-0017 built `--coverage-analysis` in three steps and deliberately left the default for last,
"only after steps 2 to 4 have run on real projects". Step 4, the command runner's own selection
contract, is not built, so `--runner command` can use `all` and not `per-file`. Deciding without it
is still sound, in one direction only: step 4 can add a setting a runner can use, never take one
away, so it can only make the case for turning something on stronger. This decision turns nothing
on. A decision to change the default would have to wait for step 4; a decision to leave it alone
does not, and the missing step is itself one of the reasons below.

The option has three settings.

- `off` runs the whole test suite once per mutant. That is what gdmutant has always done.
- `all` runs the suite once more up front, on a throwaway copy of the project with a marker in
  front of every mutated statement, and reports a mutant whose statement no test reached as
  `no coverage` without running it.
- `per-file` does that with two marker passes instead of one, and then runs every other mutant
  against only the test files that reach it.

Three things had to be measured to choose between them: how many mutants a run needs before the
up-front cost pays for itself, what a user loses by leaving it off, and what a user risks by having
it on.

### How this was measured
One machine, Windows 11, 16 cores, Godot 4.7-stable (console build, non-Mono), gdmutant at
`e4bb58f`, the commit that finished step 3. Every project was copied first and measured in the copy,
so nothing
measured touched a working tree. Every run was serial and alone on the machine unless it says
`--jobs 4`.

Three real projects of different shapes:

| Project | Runner | Test files | Tests | Whole suite |
|---|---|---|---|---|
| GdUnit4's own repository | `gdunit4` | 127 suites | 1,569 | 110.5 s |
| GUT's own repository, 9.7.1 | `gut` | 61 files under `test/unit` | 1,524 | 125.0 s |
| A private game project | `command` | 33 | not reported by an exit code | 2.0 s |

Two changes were needed before any of it. GdUnit4's `GdUnitSceneRunnerTest` drives UI input, which
headless Godot does not deliver, so it failed 17 tests and gdmutant refused to start on a red
baseline. It was removed from the copy. GUT's `.gutconfig.json` names its own test directories, and
GUT adds those to whatever `--tests` says, so `--tests` cannot narrow a run there at all. That key
was removed from the copy, which is why the counts above are `test/unit` alone.

Wall-clock was read from the run's own output, which timestamps the plan line and heartbeats every
tenth of a file. That gives the cost before the first mutant and the cumulative cost after N
mutants from a single run, rather than one total per run.

## What was measured

### The first answer was not a number
On two of the three projects, turning coverage analysis on turns a working run into a stopped one.

GdUnit4's own repository. Both `all` and `per-file` stop with exit 1 after the marker run,
because three tests that pass without markers fail with them. It is reproducible, and the cause is
not the markers. Installing gdmutant's recorder into a copy and marking nothing at all reproduces
all three failures: `GdUnitTestDiscovererTest.test_scan_test_directories` asserts the project's
top-level directory list exactly, and the recorder adds a `_gdmutant` directory to it.

```
Expecting contains exactly elements:
  'PackedStringArray(["res://_gdmutant", "res://addons", "res://assets"])'
```

Marking one module gives three failures. Marking the whole `src` tree gives seven. Removing that one
test file makes a single-module run work, and a whole-tree run still stops on three other failures,
so this is not one test standing between that repository and the feature.

GUT's own repository. Both settings stop with exit 1, on a different rule: the marker run's
output must hold no Godot runtime error, because a runtime error aborts the function it happens in
and can make code a test reaches look unreached. GUT's own suite produces them on purpose.

```
SCRIPT ERROR: Parse Error: Unexpected identifier "asdf" in class body.
   at: GDScript::reload (res://addons/tests/5/test_create_script_from_source_1.gd:1)
```

That is `test_dynamic_gdscript.gd`'s `test_when_script_source_invalid_the_error_code_is_returned`,
which compiles invalid GDScript deliberately to check the error code that comes back. Removing that
one file does not fix it. The next run stops on a second deliberate error, in `test_error_tracker.gd`,
which divides an integer by a string to give its error tracker something to track. A sweep of one
whole suite run counted 58 runtime-error frames from those two test files.

This is not an unlucky pair of projects. A test framework's own suite tests its error handling, so
it makes errors. Any project with a test for "what happens when this input is bad" can be in the
same position.

The private game project is the one that clears the gate. Twelve consecutive runs of its suite
produced no `SCRIPT ERROR` at all, and its marker run is clean.

A third way to be stopped turned up later, on a repaired copy, and it is the most ordinary of the
three. Run gdmutant on a single file whose only mutant sits on a line no test reaches, and no marker
fires at all. The rule "at least one marker recorded a hit" exists to catch a recorder that never
ran, and it cannot tell that apart from a file nothing reaches, so the run stops with exit 1. That
is the answer the user came for, reported as a failure.

A fourth is not a refusal but is worth knowing: a suite can depend on the order its files run in,
and gdmutant finds that out only in the reverse marker pass, after both passes are paid for. GUT's
own suite does. More on that below.

### What a run costs with the option off
`off` is exactly linear, one whole-suite run per mutant. Measured on GdUnit4's own repository, one
module, 20 mutants, with a heartbeat every two mutants. This is the copy before the repair below,
so its suite holds one test file more than the repaired copy the later tables use, and its numbers
are a couple of percent higher throughout:

| Mutants done | 2 | 4 | 6 | 8 | 10 | 12 | 14 | 16 | 18 | 20 |
|---|---|---|---|---|---|---|---|---|---|---|
| Seconds | 318.5 | 524.5 | 736.5 | 949.1 | 1156.6 | 1367.8 | 1575.6 | 1789.6 | 1996.9 | 2208.6 |

112.1 s before the first mutant (the import scan and the baseline suite), then 104.8 s per mutant,
straight to within 1.5% across the whole range.

On the private game project, the same shape at a hundredth of the scale: 4.5 s before the first
mutant, then 4.44 s per mutant over 13 mutants, against a 2.0 s suite. Most of a mutant's cost
there is Godot starting, not tests running.

### What the up-front cost is
Measured from the timestamp of the plan line, which is the last thing printed before the first
mutant runs.

| Project | `off` | `all` | `per-file` |
|---|---|---|---|
| GdUnit4's own repository (repaired, see below) | 108.9 s | 217.2 s | 322.4 s |
| The private game project | 4.5 s | 17.9 s | not available with `--runner command` |

So `all` costs about two baseline suites up front instead of one, and `per-file` about three. On the
game project the ratio is worse, 4x, because its suite is small enough that Godot's import scan is a
large part of it.

That is not the whole fixed cost. The self-check re-runs three `no coverage` mutants and three
selected mutants against the whole suite, so `per-file` pays up to six more whole-suite runs that do
not shrink with the mutant count. On GdUnit4's own repository that is about 620 s on top of the 322.

`--jobs` does not help with any of it. The marker passes are serial, so on GUT's own repository the
380 s before the first mutant was the same at `--jobs 4` as at `--jobs 1`, while the mutant phase it
is measured against shrank by about 3x. On the private game project the same shape: `--jobs 4` took
a 62.2 s run down to 13.4 s with the option off, and a 73.8 s run down to 18.5 s with `all`, so the
13 s of marker run went from a fifth of the run to a third of it. Coverage analysis and parallel
evaluation are not two speedups that add up.

### What selection is worth where it runs
Since the gate stops both projects with a large suite, it was lifted on a copy of each, to measure
what the lever is worth for a user whose project does clear it. On GdUnit4's repository that meant
removing the one test file that asserts the directory list. On GUT's it meant removing the two test
files that produce runtime errors on purpose, found by sweeping one whole suite run for every frame
under a `SCRIPT ERROR`. Nothing else was changed in either.

#### GdUnit4's repository, with that one test file removed
Same module, same 20 mutants, same machine:

| Setting | Total | Against `off` |
|---|---|---|
| `off` | 2175.5 s | |
| `all` | 2261.0 s | 85.5 s slower, 3.9% |
| `per-file` | 1019.7 s | 2.13x faster |

The run's own plan line says why:

```
coverage: 3 of 20 mutants sit where no test reaches, so they need no run. 17 run only the test
files that reach them, out of 126, and 0 run the whole suite (0 marked lines were reached
differently in the two passes). The self-check runs 6 of them against the whole suite anyway.
```

A selected mutant cost 13.7 s against 103.3 s for a whole-suite one, about 7.5x, measured over the
eleven mutants between two heartbeats. That is the lever working exactly as ADR-0017 predicted, and
it is why `all` is a loss on the same module while `per-file` is a win: `all` saves only the three
runs the three unreached mutants would have cost, and hands all three straight back to the
self-check.

Every verdict agreed. `off` reported 13 killed and 7 survived. Both coverage settings reported 13
killed, 4 survived and 3 no coverage, which is the same answer with three of those seven named more
precisely.

#### GUT's repository, with both of those test files removed
Selection never ran here at all. The reverse marker pass found that the suite depends on the order
its files run in, so gdmutant refused to select and carried on with the `no coverage` half alone,
which found nothing to report.

```
coverage: 1 tests failed when the same files ran in the opposite order
(test/unit/test_autofree.gd), so this suite depends on the order its files run in. Running only
some of them could change a verdict, so gdmutant will not select tests for this run. It still
reports the mutants no test reaches.
coverage: 0 of 17 mutants sit where no test reaches, so they need no run. The self-check runs 0 of
them against the whole suite anyway.
```

Same module, same 17 mutants, and the same verdicts every time (14 killed, 2 survived, 1 error):

| Setting | Serial | `--jobs 4` |
|---|---|---|
| `off` | 2177.6 s | 686.4 s |
| `per-file` | 2434.6 s | 938.2 s |
| What turning it on cost | 257.0 s, 11.8% | 251.8 s, 36.7% |

Two things come out of that. One is that this is another distinct reason these two repositories
cannot use selection, on top of the directory-listing test and the deliberate runtime errors, and it
is the only one that is not fatal: the run finished, it was right, and it was slower. The other is
that `--jobs` makes the cost relatively worse, not better. The marker passes are serial, so the
380 s they cost does not shrink with more workers while the mutant phase they are measured against
does.

### The break-even
`per-file` against `off`, three mutant counts, same project, same copy, one file each.

| Mutants | `off` | `per-file` | What happened |
|---|---|---|---|
| 1 | 211.9 s | stopped, exit 1 after 218.7 s | The file's one mutant sits on a line no test reaches, so no marker fired, and "no marker recorded a single hit" cannot tell that from a recorder that never ran |
| 8 | 944.4 s | 1060.8 s | 12.3% slower. The map narrowed four of the eight, and the other four ran the whole suite anyway |
| 20 | 2175.5 s | 1019.7 s | 2.13x faster. The map narrowed seventeen of the twenty, and none ran the whole suite |

So on this project the crossover sits between 8 and 20 mutants. Where exactly depends less on the
count than on what the map can narrow, which is a property of the file rather than of the run:
`per-file`'s fixed cost is about 322 s before the first mutant plus up to six whole-suite runs for
the self-check, and it earns that back at roughly 90 s per mutant the map narrows and nothing per
mutant it does not.

The one-mutant row is not an edge case worth waving away. Running gdmutant on the one file you just
changed is the ordinary way to use it, and that is exactly the run with the fewest mutants to spread
a fixed cost over.

### What a user loses by leaving it off
A mutant no test reaches is reported as survived, so the report overstates how bad the tests are and
sends the reader to a line that needs a first test rather than a better assertion. The share this
affects is a property of the project, and it varies enormously.

| Project | Mutants | No coverage | Share |
|---|---|---|---|
| The private game project, whole `src` tree | 1,589 | 590 | 37.1% |
| The private game project, one map-generation module | 20 | 0 | 0% |
| The private game project, one small rendering module | 8 | 6 | 75% |
| GdUnit4's own repository (repaired), one module | 20 | 3 | 15% |
| GUT's own repository (repaired), whole `addons/gut` tree | 7,235 | 3,000 | 41.5% |

The 37.1% is the number that matters most here. On that project more than a third of every
`survived` line in a whole-tree report is really "no test runs this at all". The small rendering
module makes it concrete: with the option off, that file reports two killed and six survived. With
it on, it reports two killed and six no coverage, in 32.4 s against 33.3 s. Same score, same time,
and a reader who now knows those six lines need a first test rather than a sharper assertion.

Against that, the map-generation module in the same project has none, and paying 13 s of marker run
to be told so is a straight loss. Note too that a low share is not good news about the tests. The
eight-mutant GdUnit4 file in the break-even table had none unreached and all eight survived: a test
file loads it, so the markers fire, and nothing asserts anything about what it returns. `no
coverage` splits the undetected mutants in two. It does not find all of the gaps.

### What a user risks by having it on
The dangerous failure is a map that drops the one test file that could kill a mutant. The mutant
then reads as survived, the score reads lower than the truth, and nothing else in the run says a
word. Nine things stand in the way. Four of them fired during these measurements, which is the point
of listing what each one is for.

| Protection | What it catches | Seen here |
|---|---|---|
| Every test passes in the marker run | A failing test may have stopped before the spot, so the spot looks unreached | Stopped both coverage settings on GdUnit4's own repository |
| No runtime error anywhere in the output | A runtime error aborts its function, so code after it looks unreached | Stopped both settings on GUT's own repository |
| The marker run's test count equals the baseline's | A marker run that is not the same suite the baseline ran | Not triggered |
| The hits file exists, parses, and holds at least one hit | A recorder that never ran, which would make every spot unreached | Stopped a one-file run whose only mutant genuinely sits where nothing reaches |
| At least one test file opened a window, and as many as the run's own report names | A "test file started" hook that never fired, which would make every spot load-time code | Not triggered |
| The reverse pass ran exactly the files it was given, in that order | A runner that ignores its file list and quietly runs everything while the summary reports a saving | Not reached here, because an earlier rule stopped GUT's run first. The configuration file it guards against is real and present in GUT's own repository, and had to be edited before a run could get that far |
| A spot credited to different files in the two passes runs the whole suite | Deferred work crossing from one test file into another | 0 spots on the one project measured |
| The first kill from a set of files is confirmed against unmutated source | A test that fails only because a file it depended on was not selected | 0 order-coupled kills on the one project measured |
| The self-check re-runs a sample both ways | A map that is wrong in a way none of the above can see | 6 mutants per run, all agreed |

If every one of them missed, what a user sees is a clean, green run whose score is too low and whose
survivor list holds a mutant a test already kills. There is no marker in the output that says so.
That is the shape the self-check exists for, and ADR-0017 records why: Infection's per-test map
broke on a framework upgrade and its kill rate fell from 90% to 42% with nothing saying so.

One risk that was ruled out by measurement: a project that turns GDScript warnings into errors.
Both scripts gdmutant injects compile clean with every warning Godot 4.7 names set to error.

### Failure modes for users who cannot benefit
| Their project | What they see with the default off | What they would see with it on |
|---|---|---|
| A harness that ignores the file list, such as GUT with a `dirs` key in its config | Nothing | Two marker passes paid, selection refused with the config file named as the usual cause, the run continues with `no coverage` only. Not reached in these measurements, since GUT's run stopped on an earlier rule, but the two marker passes cost about 380 s on that repository whatever comes after them |
| An order-dependent suite | Nothing | The forward pass is clean, the reverse pass fails, selection is refused for the run and the failing files are named. GUT's own suite, measured: 11.8% slower serial and 36.7% slower at `--jobs 4`, for identical verdicts |
| A suite that produces Godot runtime errors on purpose | Nothing | The run stops, exit 1. One of the three projects measured |
| A project with a test that asserts its own directory listing | Nothing | The run stops, exit 1. One of the three projects measured |
| One file whose mutants all sit where no test reaches | Nothing, beyond the survivors it already reports | The run stops, exit 1, because no marker fired at all |
| A tiny suite where Godot startup dominates | Nothing | About 13 s of fixed cost against 4.4 s per mutant, and selection cannot help because the test time it saves is a fraction of a second |
| Any project, with `--runner command` and no Godot on the path | Nothing | Coverage analysis needs `--godot` with every runner, `--runner command` included, so a working command-runner setup that never needed Godot named now does |

## Decision
`--coverage-analysis` keeps defaulting to `off`.

The measurement says three things, and any one of them is enough on its own.

1. On two of three real projects, a default of `all` or `per-file` turns a working `gdmutant run`
   into exit 1, and a third way to be stopped showed up on a file with one unreached mutant. Every
   refusal is the tool being right: a marker run with a failing test or a runtime error in it cannot
   be trusted to say what no test reaches. But a default whose first act on a real project is to
   stop the run is worse than no default at all, and none of the causes is something a user did
   wrong. One is a test asserting its own project's directory list, one is a test checking what
   happens when input is bad, and one is a file with no tests, which is the thing the feature exists
   to find.
2. `all` is a loss on wall-clock unless a large share of mutants are unreached. It trades one
   whole-suite run per unreached mutant for a marker run, an import scan and up to three self-check
   runs. Measured on a 20-mutant module with three unreached, it cost 85.5 s more than
   `off` for exactly the same verdicts. Its value is the honest verdict, not speed, and that value
   is worth asking for rather than being given.
3. `per-file` is a real win where it runs, and it cannot be the default anyway. It is refused
   outright for `--runner command`, one of gdmutant's three runners, because an exit code cannot
   say which tests ran. A default that a third of the runners reject is not a default.

The rule this follows is the one ADR-0017 took from cargo-mutants: prefer always-correct to fast,
and keep the fast path opt-in until the evidence is in. The evidence is in, and it says the fast
path is worth having and not worth imposing.

## Alternatives considered

### Default to `all`
The case for it is real. On the one project whose marker run is clean, 37.1% of a whole-tree run's
mutants sit where no test reaches, and every one of them is reported as a survivor today. That is a
third of the report pointing at the wrong fix.

Rejected because the same project is the only one of three where it would have worked, and because
`all` is not free. It costs two baseline suites up front instead of one, plus three whole-suite
self-check runs, against a saving of one whole-suite run per unreached mutant. On a module with
three unreached out of twenty it was slower than `off` for the same answers. A user who wants the
honest verdict can ask for it in five words.

### Default to `per-file` where the runner supports it, `all` elsewhere
Rejected for everything above, plus one more: a default that means something different depending on
which runner is configured is a default nobody can state. It would also mean a `--runner command`
user's default silently includes an extra requirement, `--godot`, that their setup never needed.

### A first-run message measured from the user's own project
The appealing version of this, "here is what the flag would buy on your project, measured", needs a
marker run to know, and the marker run is the thing that fails on two of three projects. So the
measured version cannot be a first-run message. It is a second run.

The unmeasured version costs nothing and is worth keeping on the table: after a run finishes with
survivors, one line saying that some of them may be lines no test reaches, and that
`--coverage-analysis all` tells the two apart. It is not built here, because this ADR is a decision
about a default and a message is a feature, and because the 0% share on one of the three modules
measured says the line would sometimes be advertising nothing. It is named under
[When to revisit](#when-to-revisit) rather than lost.

### Keep `off` but make the clean-run rules softer so the default could be on
Rejected, and not close. The rule that stopped GUT is the one that stops a runtime error from
turning reached code into unreached code, which is the exact failure that produces a false survivor.
Loosening a correctness rule so that a default can be turned on is trading the thing the feature is
for against the convenience of not typing it.

## Consequences
- Nothing changes in the tool. `--coverage-analysis off` stays the default, and the three settings
  keep the behaviour ADR-0017's updates describe.
- A user who wants the honest `no coverage` verdict, or the speedup, asks for it. The guide says
  what each setting costs and what stops a run.
- The measurement here is the baseline for the next time this is asked. Every refusal seen has a
  named cause rather than being a surprise, and the ones that would have to go are listed under
  [When to revisit](#when-to-revisit).

## When to revisit
Any of these, and this decision is worth reopening.

- The recorder stops being visible to the project under test. The GdUnit4 failure is not about
  markers at all. It is a directory gdmutant adds. A recorder that leaves no trace a test can see,
  or a documented way to exclude it, removes that refusal outright.
- A run with no marker hits can be told apart from a recorder that never ran. Today they share one
  rule and one exit code, and the innocent case is a file nothing tests, which is the answer the
  feature exists to give. The recorder writes its file whether or not it recorded anything, so the
  two are distinguishable in principle. Until they are, coverage analysis cannot be on by default
  for anyone who runs gdmutant one file at a time.
- A project can say which runtime errors it expects. The GUT failure is a correct rule meeting a
  suite that produces errors on purpose. A way to name those, per project, the way
  `# gdmutant: ignore` names an equivalent mutant, removes that one without softening the rule for
  anyone else.
- The marker run is clean out of the box on several more real projects. Two of three is the
  measurement here. If that ratio turns out to be a property of framework repositories rather than
  of projects in general, the number to beat is a clear majority, measured, not assumed.
- The self-check stops being a fixed cost. Six whole-suite runs is most of what makes `per-file`
  lose at small mutant counts. A cheaper tripwire with the same reach would move the break-even a
  long way down.

Until then the one cheap improvement that needs none of the above is the unmeasured first-run
message under [Alternatives considered](#a-first-run-message-measured-from-the-users-own-project).
