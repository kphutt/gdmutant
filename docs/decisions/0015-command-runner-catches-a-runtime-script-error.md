---
type: decision
status: active
created: 2026-08-06
---

# CommandRunner catches a runtime SCRIPT ERROR, in the engine, not behind a new flag

## Status
Accepted

## Context
Running gdmutant's own `--runner command` against a real, private Godot project's hand-rolled
headless test harness (the driver project referenced in ADR-0005) surfaced a false survivor:
GDScript has no exceptions, so a runtime error (a null access, an out-of-range index, …) aborts only
the *current function call* at that exact statement — Godot logs `SCRIPT ERROR` and keeps running
everything else, the process does not crash. A harness whose pass/fail check only reads its own
recorded assertion failures (never "did every test method actually reach its end") cannot see this: a
test that errors out halfway through, before reaching its own assertions, prints nothing that looks
like a failure and exits 0. Concretely, a mutant that deleted a null-guard produced exactly this, and
`CommandRunner` reported the mutant SURVIVED — the exit code it read really was 0, so `CommandRunner`
did nothing wrong by its own contract; the contract itself has this blind spot.

A first version of this fix (reverted; its PR is now `git revert`ed on `main`) added a whole new
adapter-scoped runner and CLI surface: a `GodotCommandRunner`/`GodotScriptRunner` class, a fourth
`--runner godot-command`/`godot-script` choice, a new `--command`/`--script` flag, and matching
changes across the README, the CLI guide, `action.yml`, and `.gdmutant.toml`'s validated keys. That
kept the engine's language-neutrality intact (`AGENTS.md`: "no GDScript-specific assumptions in
`gdmutant/engine/`"), and a pre-merge review even confirmed it worked, but it asked every user of a
hand-rolled Godot harness to discover and switch to a new flag to get a correctness fix — real cost
for something that should just be true of `--runner command` already.

## Decision
Add the check directly to `CommandRunner.run()`, in `gdmutant/engine/runner.py`: after the command
completes, scan its captured stdout+stderr for the literal string `SCRIPT ERROR` and report it via
`SuiteResult(errors=1)` regardless of exit code, before the ordinary exit-code check. No new class, no
new `--runner` choice, no new flag, no documentation change beyond this ADR and the one-line
`CHANGELOG.md` entry — a project already using `--runner command` for a Godot harness gets the fix for
free, with an unchanged command line.

This is a deliberate, narrow exception to `AGENTS.md`'s engine-language-neutrality rule, not an
accidental one. `CommandRunner`'s docstring and `AGENTS.md` itself now say so explicitly, so it reads
as an intentional trade-off rather than an unexplained inconsistency the next reader has to puzzle out.

## Consequences
- Any project already using `--runner command` against a hand-rolled Godot harness gets the
  correctness fix automatically, with zero changes to its own command line or CI config.
- `CommandRunner` is no longer 100% language-neutral. The check is a single string match on output
  that already exists (nothing new is executed, nothing new is required as input), and it can never
  fire for a non-Godot command — the marker string simply never appears — so it costs those callers
  nothing beyond the one comparison. But it is a real, permanent asterisk on the class's own claim to
  be generic, and a hypothetical second, non-Godot adapter would carry a dead check it can never use.
- The cold `.godot/` import-cache trap (the first run silently imports every asset before any test
  runs, discussed alongside this fix) is explicitly **not** addressed here. It was never a
  correctness bug — the existing `_cold_import_notice` in `cli.py` already tells the user exactly what
  to do — and fixing it properly needs a way to know which binary is Godot, which `--runner command`'s
  opaque `--command` string does not provide without either a new flag or guessing from `command[0]`.
  Both were rejected as out of scope for this fix: real tools with the same shape of problem
  (Playwright's browser install, Docker's image pull, Gradle's dependency resolution) commonly choose
  "tell the user, let them run it once" over silently absorbing the cost inside an unrelated command,
  and that is what gdmutant already does here.
- No new ADR-0014 reuse: the prior godot-command/godot-script PR's ADR-0014 was deleted along with the
  rest of that revert. This is `0015`, not a reused `0014`, since that PR's own (now-historical) review
  comments already reference `docs/decisions/0014-godot-aware-command-runner.md` by that exact path —
  reusing the number would point old references at unrelated content.
