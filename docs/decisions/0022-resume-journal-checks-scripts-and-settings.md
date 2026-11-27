---
type: decision
status: active
created: 2026-11-26
---

# A resume journal is refused when a script, a verdict setting or the version changed

## Status
Accepted, and built. Shipped in 0.1.8 with `--resume`, which @normanthesquid contributed in
[#379](https://github.com/kphutt/gdmutant/pull/379).

## Context

`--resume <file>` writes each verdict to a journal file. Run the same command again after a stop,
and gdmutant skips every mutant the journal already has a verdict for.

That is only safe if an old verdict still says something true about the code now. A verdict is
"this mutant, in this code, under this test run, was killed or survived". Change the code or the
test run, and the old verdict can be wrong. A resumed run would then write one report that mixes two
states of the project, and nothing in the report would show it.

So the journal has to decide, before it reuses anything, whether the project is still the one it
was written for.

## Decision

The first line of the journal is a header. It holds a hash or value for each input below. On
resume, any difference refuses the journal with exit code 2. The message names each difference.
`--jobs` is not recorded, so it may differ.

### What is checked

- The source files being mutated: which files, and a hash of each (`sources` in the header).
- Every other script and shader in the project, tests included, by hash (`scripts`). That means
  files ending in `.gd`, `.gdshader` or `.gdshaderinc` (`_PROJECT_SCRIPTS` in `gdmutant/cli.py`).
  Folders whose names start with `.`, such as `.godot`, are skipped.
- The settings that can change a verdict (`settings`): the runner, `--tests`, `--godot`,
  `--command`, `--timeout`, the coverage analysis settings, `--exclude-tests` and `--since`. Also
  the output of `godot --version`, but only when gdmutant runs Godot itself (the GdUnit4 or GUT
  runner, or coverage analysis). Otherwise that entry reads `not used`.
- The gdmutant version, and the journal format number.

### What is deliberately not checked

Scenes, resources and `project.godot` are not hashed. Folders that start with `.` are not walked.

The reason stated in the PR is that suites and Godot itself rewrite scenes, resources and
`project.godot`. A hash of such a file would change between a run and its own resume, and the
journal would refuse for no real reason. The code comment on `_PROJECT_SCRIPTS` says the same:
scripts are the files neither a suite nor Godot rewrites.

The PR does not say why hidden folders are skipped. The code comment names Godot's `.godot` cache as
the example. No other reason is recorded.

### Verdicts are written one run behind

A verdict is not written when it lands. `_Recorder` in `gdmutant/engine/loop.py` holds it, and
writes it only once a later run has ended. It never writes once gdmutant has begun to stop
(`commands_stopping`). The last verdict of a run is written by `flush`, once the run ended normally.

The reason, from the `_Recorder` docstring: a stopped run can kill suites that are still going,
and what a killed suite reports looks like a verdict. Holding each verdict back keeps a suite cut
short by Ctrl-C from being saved as a real one. The cost is that a run that stops loses the last
verdict of each worker, as well as the mutants still running.

### Other rules

- The baseline runs again on every resume, and so do coverage analysis's marker runs.
- A mutant whose run failed (`error`) runs again.
- The header is written through a rename. A half-written last line is dropped, but only after the
  file is known to be a journal.

## Alternatives considered

The PR discussion does not weigh any other design for what to check. It does record two changes
made in review:

- The `--help` text first said a changed script, scene or resource refuses the journal. The code
  hashes only scripts and shaders, so the maintainer asked for the help to say so, and the
  contributor changed it.
- The maintainer suggested recording the Godot version string, since a Godot upgrade at the same
  path would otherwise go unseen. The contributor added it.

## Consequences

- A stale verdict is refused loudly for the checked inputs, with a message that names the file or
  setting.
- Known gap: change a scene, a resource or `project.godot`, then resume, and gdmutant does not
  notice. It can reuse a verdict that the change would have altered, and the report mixes two
  project states. The guide tells users to start a new journal after changing one of those files.
  Nothing enforces that.
- A killed serial run can leave a mutant in the source file it was mutating. That file then counts
  as changed, so the journal is refused until the file is restored. The refusal message says so.
- The check hashes every script in the project on each resume, so the cost grows with project size.

## When to revisit

- If scenes or resources prove to change verdicts often enough to matter. They would need a hash
  that ignores what suites and Godot rewrite, which this ADR does not have.
- If a new input starts to affect verdicts. It has to join the header, or the journal will reuse
  verdicts it should not.
