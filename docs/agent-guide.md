---
type: guide
status: active
created: 2026-07-11
---

# Driving gdmutant from an AI agent

A one-read guide for an AI agent to run gdmutant and act on the results correctly. This is a
*how-to-use-the-tool* guide for consumers — distinct from
[`AGENTS.md`](../AGENTS.md), which is for contributors *to* gdmutant.

## Install

gdmutant is a Python CLI (**Python 3.12+**), not on PyPI yet — install from git at a pinned commit
with uv:

```sh
uv add "git+https://github.com/kphutt/gdmutant@<commit-sha>"
```

For a Godot project with no Python of its own, put that in a tiny **non-package** uv project beside
the game and pin the interpreter (a `.python-version`) so uv doesn't resolve to a newer system
Python — full recipe in the README's "Install into your project" section. Then `uv run gdmutant …`.

## Invoke

```sh
gdmutant run <file.gd> --project <godot-project-dir> --json -
```

- `--json -` streams the machine-readable report to **stdout**; the human summary and per-mutant
  progress go to **stderr**. Capture stdout for parsing; stdout stays pure JSON.
- `--dry-run` lists the mutants gdmutant *would* generate, without Godot and without running any
  tests — a fast, dependency-free preview.
- `--require-clean` refuses to run if the source file has uncommitted git changes (exit 2).
  Without it, gdmutant only *warns* and proceeds (it never blocks on a prompt — safe for headless
  agents).
- Other flags: `--tests res://test`, `--godot <path>`, `--report-path <rel>`, `--timeout <seconds>`.
  `gdmutant run --help` lists them all.
- **Runner selection.** `--runner gdunit4` (default) and `--runner gut` are first-class peer JUnit
  adapters (per-test detail). **No JUnit XML?** `--runner command --command "<test cmd>"` — any
  command that exits non-zero on failure (e.g. a hand-rolled
  `godot --headless --script res://tests/run_tests.gd`). See
  [`docs/decisions/0011`](decisions/0011-runner-agnostic-adapter-seam.md) (the runner seam) and
  [`docs/decisions/0005`](decisions/0005-exit-code-test-runner-convention.md) (the exit-code fallback).

## Exit codes (the contract)

- **`0`** — the run completed. **Survivors are normal output, not a failure** — parse them.
- **`1`** — the unmutated *baseline* suite failed. Fix your tests first; mutation-testing a red
  suite is meaningless.
- **`2`** — a setup/input error: the source is unreadable or not valid GDScript, `--project`
  doesn't exist, `--require-clean` was set on a dirty tree, the test-runner executable (`godot`)
  wasn't found, or the report couldn't be written. The stderr message says which.

## Safety guarantee

gdmutant mutates the source file **in place**, then restores it to its original bytes in a
`finally` — after every mutant and on a normal exit or Ctrl-C. Your working tree is returned
unchanged. The only way a swap can persist is a hard kill (SIGKILL / power loss), so commit or
stash first, or pass `--require-clean`.

## Output schema (Stryker `mutation-testing-elements`, v2)

`--json -` emits one report object:

```json
{
  "schemaVersion": "2",
  "thresholds": {"high": 80, "low": 60},
  "files": {
    "corpus/turn_order.gd": {
      "language": "gdscript",
      "source": "<full file source>",
      "mutants": [
        {
          "id": "0",
          "mutatorName": "comparison",
          "replacement": ">=",
          "location": {"start": {"line": 8, "column": 17}, "end": {"line": 8, "column": 18}},
          "status": "Survived"
        }
      ]
    }
  }
}
```

- `status` is one of `Killed`, `Survived`, `Timeout` (the mutation hung the suite — a detection, so
  it **counts as killed**), `Ignored` (a `# gdmutant: ignore` annotation suppressed it — **excluded
  from the score**; its reason, if any, is in `statusReason`), `CompileError` (the mutant didn't
  parse — **never** counted as killed), or `RuntimeError` (the runner failed to execute it, e.g. a
  Godot crash).
- Locations are **1-based**; the `end` `column` is **exclusive**.
- **Actionable survivors** are the mutants with `"status": "Survived"`. Those are the gaps a test
  should close.
- Mutant order is **deterministic** (fixed generation order), so `id`s and the survivor list are
  stable across runs — safe to diff between attempts.

## The survivor → killing-test loop

1. Run with `--json -`, capture stdout, and read `files[<path>].mutants`.
2. For each `"Survived"` mutant: it gives you a `location` and the `replacement` (the exact change
   no test caught). Write or strengthen a test that **fails** under that change — usually an
   assertion pinned to the boundary or value the mutation moves.
3. Re-run and confirm that mutant is now `"Killed"`.
4. If a survivor is a genuine **equivalent mutant** — one that *cannot* change observable behavior
   (e.g. a clamp whose boundary can't be reached) — annotate its line so it becomes `Ignored`
   (excluded from the score) and your fixer loop terminates:
   - `# gdmutant: ignore` — suppress **every** mutant on the line.
   - `# gdmutant: ignore[comparison]` — suppress only that operator's mutant(s) on the line, when a
     killed or timeout mutant shares the line (use the `mutatorName` from the report). Comma-list
     several: `ignore[comparison, numeric]`.
   - Trailing text is the **reason** (surfaced as `statusReason`): `# gdmutant: ignore[comparison]
     equivalent at the boundary`.

   Only suppress *proven* equivalents (or benign, brittle-to-kill mutants — with a reason). See
   [`docs/decisions/0004`](decisions/0004-equivalent-mutant-ignore-annotation.md) and
   [`0006`](decisions/0006-operator-scoped-ignore-and-ignored-status.md).

A well-behaved fixer loop terminates because every survivor is either killed (step 3) or suppressed
as equivalent (step 4) — never retried forever.

For a fuller, copy-pasteable version of this loop with a real worked example (a killable survivor
and a genuine equivalent from the bundled corpus), see
[`mutation-fixer-recipe.md`](mutation-fixer-recipe.md).
