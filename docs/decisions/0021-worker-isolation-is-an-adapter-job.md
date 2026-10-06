---
type: decision
status: active
created: 2026-10-05
---

# Worker isolation is the adapter's job, behind a neutral engine seam

## Status
Accepted, and built. Under `--jobs N` the engine hands every project copy to the language adapter
before a worker runs anything in it, and the unmutated baseline runs in an isolated copy too.

## Context

`--jobs N` evaluates N mutants at once by giving each worker its own copy of the whole project and
mutating the file inside that copy. [The design](../design/DESIGN.md) said of that path only that
"workers can never collide on one file and the real source is never written to on that path". That
sentence is true, and its subject is files inside the project directory. A test run is not only
files inside the project directory, and the gap between those two statements hid a defect that
scored a run 100% while every mutant in it was killed by a broken suite rather than by a test.

Godot works out where `user://` is from the project's *name settings*
(`application/config/name`, and the custom-user-dir keys beside it), never from the project's path.
So N copies of one project all resolve `user://` to the same single directory on the machine. A
copy is not isolation.

That cost the tool two separate ways, and the second one only appeared once the first was fixed.

### A shared `user://` turns a survivor into a kill

A suite whose tests write a fixed-name file under `user://` has its workers overwrite each other
the moment two of them overlap. The suite goes red for a reason that has nothing to do with the
mutant, and a red suite is a kill (FG-4.1). The error runs one way only: the survivor list comes
out shorter than the truth and the score comes out better than the truth. That is the same failure
NF-5 puts at the top of the list of things never to ship, a silently wrong survivor report, reached
through concurrency rather than through a bad mutant.

### A private `user://` reopened the hole through the baseline

Give each worker its own `user://` and it starts empty, because nothing has written to it yet.
The baseline still ran in the project's own directory, with the machine's real `user://` in place.
A suite that *reads* data under `user://` that no test in the run creates is therefore green where
the baseline ran and red in every worker, and red is still a kill.

Measured against real Godot, before the baseline moved: one project, one mutation target nothing in
the project calls, and a suite that reads a file under `user://` no test creates. `--jobs 1` scored
0.0% and listed both survivors. `--jobs 4` scored 100.0% with no survivors, exit code 0, and no
warning anywhere in the run.

So the design question is not only how `user://` gets isolated. It is who is allowed to know what
`user://` is, and where the baseline runs.

### The constraint that shapes the answer

`AGENTS.md` and NF-3 say the engine holds no language specifics: nothing under `gdmutant/engine/`
may know about Godot, about `project.godot`, or about `user://`. The engine is the only part that
knows a parallel run is happening, and it is the part forbidden to know what the isolation consists
of.

### Why the obvious check is worth nothing here

Running the corpus twice at `--jobs 4` and comparing the two reports passes while the defect is
live, measured over 25 corpus runs with no disagreement, because no corpus test writes to `user://`
at all. That is `AGENTS.md`'s recurring bug one, a gate that passes without checking anything, so
whatever proves this has to bring its own colliding suite and compare against a serial run rather
than against a repeat of itself.

## Decision

Worker isolation is a language-adapter responsibility, reached through a neutral seam on the engine,
and the baseline runs wherever the mutants run. Three parts.

### 1. Two fields on the `Adapter` seam

`engine/adapter.py` gains `isolate_copy(copy_dir, token)` and `release_copy(token)`. The engine
makes the copy, assigns a token that is unique to that worker on that machine, hands both over, and
asks. It never learns what the isolation consists of.

- `isolate_copy` is called before that worker's thread starts. A suite run against a copy that
  is not yet isolated is exactly the collision this exists to stop.
- Raising is the right answer when a copy cannot be isolated. The engine would otherwise go on and
  run it in parallel, which is the defect.
- A no-op implementation is allowed, for a language whose test run genuinely shares nothing outside
  the project directory. It is a *claim* that parallel runs are sound, not an absence of opinion.
- `release_copy` runs once per isolated copy, from a `finally`, after that worker's last suite run,
  including when the run is failing. The engine deletes the project copy itself. This is for what
  the isolation created *outside* the copy, which the engine has no way to name.

The fields are named after what they are for rather than after `user://`, because the state a
language shares outside the project directory differs by language. The field documentation names the
class: a location derived from the project's name rather than its path, a fixed port, a directory in
the user's profile.

The isolation sits on the `Adapter` seam (NF-3, per language) and not on the `Runner` seam
([ADR-0011](0011-runner-agnostic-adapter-seam.md), per framework), because `user://` is a property
of a Godot project rather than of GdUnit4, GUT or a hand-rolled command. One consequence of putting
it there is that no runner can opt out of it: all three shipped runners get the same isolation from
the same adapter code path, without a line of runner code.

### 2. GDScript implements it as three project settings

`adapters/gdscript/project_settings.py` writes into the copy's `project.godot`:

- `config/use_custom_user_dir=true` and `config/custom_user_dir_name="gdmutant/<token>"`, which
  move `user://` to `<data dir>/gdmutant/<token>`. Both unconditionally, not only when absent: a
  project that already sets `use_custom_user_dir=false`, or that names its own custom directory,
  would otherwise keep every worker pointed at one shared place. Overwriting them in a throwaway
  copy costs that project nothing, since the copy lives for one mutant and is then deleted.
- `config/name`, but only when the project does not already have a non-empty one. Godot reads the
  project name *first* and ignores both custom-user-dir settings when it is empty, falling back to
  one `app_userdata/[unnamed project]` directory shared by every nameless project on the machine
  (verified against Godot 4.7). A nameless project would otherwise look isolated and not be.

The settings editor is the same `with_setting` the coverage marker run already used, moved here, so
there is one implementation rather than two that can drift.

`release_copy` deletes that worker's directory. Every worker of every run lands under one
`gdmutant/` parent inside the machine's data directory, so the leftovers of an interrupted run are
in one findable place and are safe to delete by hand.

### 3. The baseline runs where the mutants run

FG-3.3 asks the unmutated suite to pass first. The point of that check is to prove the suite is
green in the conditions the mutants will run in, because a suite that is red for its own reasons
turns every mutant into a false kill. Under `--jobs N` those conditions are an isolated copy, so
that is where the baseline runs: the engine copies the project once more, isolates the copy through
the same seam, runs the unmutated suite there, and releases it.

Agreement by construction rather than by a check someone has to remember. There is one
`_run_baseline`, and it runs wherever the mutants will.

Three details:

- `--jobs 1` is unchanged. The baseline and every mutant run in the project's own directory, which
  is what they always did.
- `--jobs auto` drops to serial, silently, for a source file that sits outside the project
  directory, and those mutants then run in the project's own directory. The baseline follows them,
  read off the same function the mutate path decides with, so the two cannot drift into
  disagreement. An explicit `--jobs N` raises for such a source instead of downgrading, so it keeps
  the isolated baseline.
- Every `BaselineFailed` from an isolated baseline carries a note saying the suite ran in an
  isolated copy and what that means. The isolation is the only thing that can make this baseline red
  where the project's own directory is green, so without the note the reader sees a suite that
  passes everywhere they try it and a tool that refuses to run, with nothing linking the two.

## Alternatives considered

### Seed each worker's `user://` from the machine's real one

Copy whatever is already in the project's `user://` into each worker's private one. A suite that
reads data no test creates would keep working, and the baseline could have stayed where it was.

Rejected, for two reasons that are both about what a report is worth.

It imports machine state into a run whose value is that it is reproducible (NF-1). The same project
would score differently on a developer's machine and in CI, and neither report would show why,
because the cause is whatever happened to be sitting in that directory. A score that depends on
unrecorded local state is a score that cannot be diffed over time, which is the whole reason NF-1
is a requirement.

And the size is unbounded. `user://` is where a Godot project keeps saves, logs and caches, so the
copy is however much the project has accumulated, once per worker, per run.

The suite this would rescue depends on state it does not set up, which is the same fault the
isolation exists to stop one worker inflicting on another. The answer is to make the suite create
what it reads, or to pass `--jobs 1`.

### Isolate in the engine

Write the three settings from `engine/loop.py`. Rejected: `project.godot` is GDScript, and NF-3
says a new language must not require touching the engine. An engine that knows how to isolate a
Godot project would have to grow a second way to isolate the next language, in the same place.

### Detect a suite that reaches outside the project, and refuse to run it in parallel

Inspect the project and its tests for anything touching `user://`, and fall back to serial when
found. Rejected as strictly weaker. It has to enumerate every way a dynamic language can reach
outside the project directory, in advance, from source, and the safe behaviour when the detection is
unsure is to refuse `--jobs` to everyone. Isolating the state makes the question moot instead of
answering it.

## What is isolated, and what is not

Stated plainly, because an implied limit is a limit nobody checks.

### A `--command` harness writing outside `user://` is still shared

The seam's field documentation names the class of state it exists for, and the GDScript
implementation covers `user://` and nothing else. The framework-neutral command runner
([ADR-0005](0005-exit-code-test-runner-convention.md)) runs an arbitrary command, and such a
harness can write to a fixed machine-wide path that is not under `user://`: a temp file with a fixed
name, a lock file, a fixed port, a database in the user's profile. Those are still shared across
workers, which is a live route to a false kill, by exactly the mechanism this ADR fixed for
`user://`. It is open, and it is tracked as work rather than closed here.

A copy with no `project.godot` at all is left alone, which is a decision rather than an oversight:
without one there is no Godot project, so there is no `user://` for anything to share. That is
reachable, because the command runner can drive a project that is not a Godot project. It is correct
for `user://`, and the paragraph above is the rest of the story.

### The coverage marker run is not isolated, and that is sound

The coverage marker run ([ADR-0017](0017-markers-for-no-coverage-and-test-selection.md)) copies the
project, marks it, and runs the suite there, keeping the project's own name settings. So it sees the
machine's real `user://`, not an empty one. It runs once, serially, before any worker starts, so it
cannot collide with a worker. What is left is divergence: it observes an environment the workers do
not have. Every route out of that divergence ends in a loud failure or in *more* survivors.

- A suite that needs real `user://` state fails the isolated baseline, and the run stops with a
  message before the marker run happens at all. The baseline runs first.
- A marker run whose suite is red, or whose test count differs from the baseline's, is refused with
  `CoverageRunFailed` (`engine.coverage.clean_run_problems`). So a suite that behaves differently in
  the two places says so out loud rather than quietly producing a map.
- A spot the marker run records as unreached becomes the `no coverage` verdict, which is scored as
  survived and never as detected. Being wrong in that direction adds to the undetected count.
- A mutant that runs only its selected test files is reported killed off those files only after the
  same set has been run unmutated inside a worker's own isolated copy (`_Trust`, whose answer is
  cached per set of files). A set that is red unmutated there does not yield a kill: the mutant runs
  the whole suite instead, and that verdict stands. In a run that got past the baseline the whole
  suite is green in an isolated copy, so the fallback is sound.
- The coverage self-check, on the mutants it samples, takes both the selected verdict and the
  whole-suite verdict inside the worker and stops the run when the two disagree.

So the marker run cannot turn a survivor into a kill. Isolating it is not needed for that, and it is
not done. Coverage analysis is also off by default
([ADR-0018](0018-coverage-analysis-stays-off-by-default.md)), so none of this arises unless it is
turned on.

### `release_copy` must not raise, and nothing checks that it does not

The engine calls it from a `finally` and does not catch what comes out. It is a contract the engine
relies on and does not enforce. The reasoning is that the mutants are already scored by then, and a
leftover temporary directory is not a reason to turn a finished run into a failed one. The GDScript
implementation deletes with errors ignored and builds the path from the run's own token, so there is
nothing else it can name. A future adapter that breaks the contract turns a complete, fully scored
run into a traceback.

### `godot_data_path` is a second copy of a rule Godot owns

Finding the directory Godot puts `user://` in means restating `OS::get_data_path` in Python. It is
only ever used to delete a directory this process asked Godot to create, so the whole cost of the
two drifting apart is a leftover directory, never a wrong verdict and never a deletion somewhere
else. The live gate is what keeps them honest, the only way that can be checked: after a parallel
run it requires that the parent directory *exists*, meaning Godot put a worker's `user://` where
this function says it would, and that it holds nothing new, meaning every worker's directory was
released. Checking only the second half would pass by default if the first were wrong.

## What is proven, and by what

- Godot-free unit tests cover the settings editing, the nameless-project case, the
  no-`project.godot` case, the engine's isolate and release calls on both paths that isolate, and
  where the baseline runs for each combination of `--jobs` and source location.
- One live gate, `tests/test_selftest_parallel_determinism.py`, drives the shipped CLI against real
  Godot. It builds its own fixture: a suite that writes a fixed-name file under `user://` and
  asserts on its contents, a mutation target whose nine mutants must all survive because nothing in
  the project calls it (checked mechanically, not asserted), and a `--jobs 1` run as the oracle that
  the parallel runs are compared against. It covers both halves, the writer and the reader. With a
  Godot configured and the GdUnit4 addon missing it fails rather than skipping, because that is the
  state where a green run has checked nothing. Only a whole-suite run with no Godot configured
  skips, and that skip raises a warning so it shows on screen.
- The live gate exercises the GdUnit4 runner only. GUT and the exit-code command runner receive
  the identical isolation from the same adapter code path, since the seam is on the `Adapter` rather
  than on the `Runner`, but no live run proves GUT's and none proves the command runner's.
- The `--since` path rebuilds the adapter to scope mutant generation, and now does it with
  `dataclasses.replace` instead of naming each field. A field listed one by one is a field the next
  one silently misses, and the symptom of missing this one is a survivor reported as killed.

## Consequences

- A new language adapter has to answer the question. Both fields are required on the `Adapter`
  dataclass, with no defaults, so an adapter cannot be constructed without stating what it isolates
  or deliberately saying "nothing".
- `--jobs 1` behaves exactly as before, and it is the default.
- A `--jobs N` run pays one extra project copy, for the baseline, against the N per file the
  parallel path already pays. The copy excludes nothing, because the workers exclude nothing: a
  cheaper copy would be a baseline for a project no worker runs.
- A suite that reads state no test creates now stops a `--jobs N` run instead of scoring it. That is
  a new refusal, and the message names the isolated copy and says what to change.
- [ADR-0011](0011-runner-agnostic-adapter-seam.md)'s drop-below-baseline guard compares a mutant
  run's test count against the baseline's. Those two numbers now come from the same kind of place.
  Before the baseline moved, a suite that collected a different number of tests with an empty
  `user://` could make a worker's count differ from the baseline's for a reason no mutant caused.
- Leftovers are findable. Everything the isolation creates lands under one `gdmutant/` directory in
  the machine's data directory.

## When to revisit

- An adapter needing to isolate something it must then communicate. The seam takes a copy and a
  token and returns nothing, which is enough for a directory. An adapter that allocates a port the
  test command then has to be told about would need the seam to return a value. Nothing needs that
  today.
- The `--command` limit above. If the command runner ever gains a way to state what its harness
  touches outside the project, the engine could refuse `--jobs N` for an unisolated harness rather
  than run it.
- A live gate for GUT and for the command runner. The isolation is shared code, so such a gate
  would be proving each runner's own behaviour against an empty `user://`, not the isolation itself.
- The marker run moving. The argument that it is sound unisolated rests on it running once,
  serially, before any worker. A future step that runs markers inside a worker, or alongside
  workers, loses that argument and has to isolate the marked copy like any other.
