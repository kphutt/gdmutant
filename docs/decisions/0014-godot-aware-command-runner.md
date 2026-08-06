---
type: decision
status: superseded
created: 2026-08-05
---

# GodotCommandRunner: a Godot-aware exit-code runner, in the adapter, not the engine

## Status
Accepted, then superseded 2026-08-06 by [`0015`](0015-command-runner-catches-a-runtime-script-error.md):
the `GodotCommandRunner` adapter class and its `--runner godot-command` CLI flag were reverted (the PR
that shipped them is `git revert`ed on `main`) in favor of the same `SCRIPT ERROR` check living
directly in `CommandRunner`, so that a project already using `--runner command` gets the fix with no
new flag to discover and no CLI/doc change to make. This record is kept as history — restored after an
earlier version of the revert deleted it outright, which contradicts this repo's own append-only ADR
convention; see 0015's own "No new ADR-0014 reuse" note. The analysis below is otherwise unedited.

## Context
[ADR-0005](0005-exit-code-test-runner-convention.md) added `CommandRunner` for any harness that
signals pass/fail via its exit code, and named it *language- and framework-neutral on purpose*, which
is why it lives in `engine/runner.py` rather than the GDScript adapter. That same ADR's
"harness-authoring caveat" section already flags one class of exit-0-despite-broken-run trap (a
harness whose entry script fails to *load*) and names a possible future fix: "a sentinel-aware runner
... behind the same `Runner` seam."

Running gdmutant's own `--runner command` against a real, private Godot project's hand-rolled headless
harness (the driver project referenced in ADR-0005) surfaced a second, distinct trap in the same
family, this time at *runtime* rather than load time: GDScript has no exceptions. A runtime error (a
null access, an out-of-range index, ...) aborts only the *current function call* at that exact
statement — Godot logs `SCRIPT ERROR` and keeps running everything else. A harness whose pass/fail
check only reads its own recorded assertion failures (never "did every test method actually reach its
end") cannot see this: a test that errors out halfway through, before reaching its own assertions,
prints nothing that looks like a failure and exits 0. Concretely, a mutant that deleted a null-guard
(`if item == null: return ...`) produced exactly this, and gdmutant reported the mutant SURVIVED —
`CommandRunner` did nothing wrong; the exit code it read really was 0.

`CommandRunner` cannot be taught to catch this without breaking the reason it exists: the fix is
recognizing the literal string `SCRIPT ERROR`, a fact about Godot, not about "a test command." Adding
it to the engine's runner would mean every non-Godot `--runner command` project silently carries
Godot-specific string matching it can never trigger and never asked for — the same category error
`AGENTS.md`'s "no GDScript-specific assumptions in `gdmutant/engine/`" rule exists to block.

The same investigation reconfirmed a second, already-known-and-documented gap:
`_cold_import_notice`'s own docstring says `--runner command` "cannot warm the [import] cache for
you: it only knows the command you gave it" — because it is handed an opaque argv with no Godot binary
of its own. Both gaps share one root cause: `CommandRunner` genuinely does not, and must not, know it
is running Godot.

## Decision
Add `GodotCommandRunner` to `gdmutant/adapters/gdscript/runner.py`, a peer of `GdUnit4Runner` and
`GutRunner`, not a change to `CommandRunner`. It implements the same `Runner` + `Preparable` contract
as the JUnit adapters: it takes `command` (the same exit-code contract as `CommandRunner`) *and*
`godot` (like the JUnit adapters), and:

- warms the import cache itself via `Preparable.prepare` (sharing `_warm_import_cache`, extracted out
  of `_GodotJUnitRunner.prepare` so both use one implementation) — so, unlike plain `command`, no
  cold-checkout notice is needed;
- scans the command's captured stdout+stderr for `SCRIPT ERROR` and reports it as an `errors` count
  (not `failures`) regardless of the exit code, with a detail message naming the mechanism (GDScript
  has no exceptions) so a first-time reader isn't left to rediscover it.

Exposed as a fourth, explicit `--runner godot-command` choice (also valid in `.gdmutant.toml`) —
not an implicit upgrade triggered by also passing `--godot` alongside `--runner command`, which would
change `command`'s behavior based on an unrelated flag's presence, the kind of "injection and weird
stuff" this project's conventions steer away from. `command` and its engine placement are unchanged.

## Consequences
- A Godot project using a hand-rolled exit-code harness gets both the cache warm-up and the
  runtime-error guard for free by switching `--runner command` to `--runner godot-command` — no
  harness changes required, since the check reads Godot's own console output, not anything the
  harness has to print itself. This is stronger than the "print a sentinel line" mitigation ADR-0005
  suggested a harness author do by hand: it costs nothing to adopt and can't be forgotten.
- `command` keeps working exactly as before, for every non-Godot project and every Godot project that
  doesn't opt in. Nothing about it changed.
- Slightly more surface area: a fourth `--runner` value, one more class in the adapter, one more
  `.gdmutant.toml` validation branch. Accepted for the same reason ADR-0011 accepted two JUnit
  adapters over one: the pattern is cheap to repeat and the alternative (teaching the engine what
  `SCRIPT ERROR` means) is the one thing NF-3 exists to rule out.
- `errors`, not `failures`, when the marker fires: a run that hit a `SCRIPT ERROR` didn't cleanly
  produce a red test, it produced an untrustworthy one, and that distinction already exists in
  `SuiteResult` for exactly this shape of problem.
- Load-time failures (ADR-0005's original caveat, a harness whose entry script won't compile) are a
  different mechanism and this does not specifically target them — though in practice a load failure
  also tends to print `SCRIPT ERROR`-adjacent diagnostics, this runner does not special-case it, and a
  harness author still benefits from ADR-0005's own guidance (the `can_instantiate()` gate) regardless
  of which runner they use.
