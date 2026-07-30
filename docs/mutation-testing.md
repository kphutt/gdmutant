---
type: explanation
status: active
created: 2026-07-11
---

# Mutation testing gdmutant itself

gdmutant is a mutation tester, so its own Python suite is held to the standard it exists to enforce:
a test suite should not just *cover* a line, it should *catch a bug* on that line. We dogfood this
with [mutmut](https://github.com/boxed/mutmut), which mutates `gdmutant/` and re-runs the suite
against each mutant. A **surviving** mutant is a change no test objected to — a gap.

## Running it

```sh
uv run mutmut run        # mutate gdmutant/ and run the suite against each mutant
uv run mutmut results    # list survivors
uv run mutmut show <id>  # show one mutant's diff
```

Configuration lives in `pyproject.toml` under `[tool.mutmut]`. mutmut runs the suite from a copied
`mutants/` tree, so the corpus fixture and `docs/` (read by the end-to-end and guide-consistency
tests) are copied in via `also_copy`, and coverage is turned off for those runs (pure per-mutant
overhead).

CI runs mutmut in **report mode** as an **advisory, non-blocking** job (`continue-on-error`, and not
a required status check): it surfaces the score on the run summary but never fails the build. It
complements the coverage gate — coverage says a line *ran*, mutation says a bug there would be
*caught*.

The module-level-only scope below (see "Scope: what the 781 covers") is measured separately, locally:
the manual pre-commit hook (`gdmutant-mutation`) runs [poodle](https://github.com/WiredNerd/poodle),
diff-scoped to files changed vs `origin/main`, which does reach class-method bodies. It's local
rather than a second CI job partly because mutmut can't run on the maintainer's Windows machine at
all (`os.fork()`), and partly because it's a targeted, on-demand check rather than a standing cost —
see `docs/decisions/0013-windows-local-mutation-testing.md` for the full reasoning, and `poodle.toml`
/ `scripts/check_mutation_baseline.py` for the config. The two tools will not report identical scores
on the same file (different operator sets, different scope); the local run's job is "did this change
to a method body just go untested," not "match CI's number."

## Current result

At the last run, **763 / 781 mutants were killed — the remaining 18 are equivalent mutants** (changes
that cannot alter observable behavior, so no test *can* catch them; this is the well-known
[equivalent mutant problem](https://en.wikipedia.org/wiki/Mutation_testing#Equivalent_mutants)).
Rather than contort the suite to "kill" them — which would only pin implementation trivia — they are
enumerated and justified below. Every behavioral mutant mutmut generates is killed (see the scope note
for what that set covers).

### Scope: what the 781 covers

mutmut 3.6 mutates **module-level functions only** — it does not generate mutants for class-method
bodies. So the 781 spans the package's 28 module-level functions (the operator catalog, spans, mutant
generation, the loop, JUnit parsing, the reporter, the CLI, the adapter), but **not** the method
bodies: `GdUnit4Runner.run`/`command`, `CommandRunner.run`, the two `replacements` implementations,
`MutationRun`'s properties, `Mutant.apply`, `Span.__post_init__`, and `SuiteResult.failed`/`passed`.
Those are covered by unit tests but not *mutation-measured* here — so read the score as "every
behavioral mutant mutmut generates is killed," over the module-level surface.

### The 18 equivalent mutants

| # | Mutation | Where | Why no black-box test can catch it |
|---|---|---|---|
| 11 | `encoding="utf-8"` → `None` / omitted / `"UTF-8"` | `engine/loop.py` `_run_one` (writing + restoring the mutated file); `cli.py` `_load_gdscript` (reading source), `run_mutation` (writing the JSON report) | `"UTF-8"` is a codec *alias* of `"utf-8"` — byte-identical. `None` / omitted falls back to the platform default text encoding, which on a UTF-8 locale (the CI runner and every environment gdmutant is used in) is itself UTF-8. Same bytes either way. The explicit `encoding="utf-8"` is still correct — it *guarantees* the equivalence across platforms instead of relying on the ambient locale. |
| 3 | `gather_metadata=True` → `False` / omitted | `adapters/gdscript/_parse` | `gather_metadata` attaches source spans to *Tree nodes*; the token line/column positions the adapter reads are set by lark's lexer regardless of the flag (verified directly). Toggling it changes no value the adapter uses. |
| 3 | defensive `assert` `and` → `or` | `adapters/gdscript/_span_of`: `assert line and col and end_line and end_col` | A type-narrowing guard for the `Optional[int]` token positions. lark always populates them (each `>= 1`, i.e. truthy), so every `and`/`or` re-association of always-truthy operands evaluates identically — the guard never trips, and no test can observe a difference. |
| 1 | `"git"` → `"GIT"` | `cli._has_uncommitted_changes` (shells out to `git status --porcelain`) | *Environment*-equivalent, not universal. On a case-insensitive filesystem (the macOS default, where this dogfood runs) the OS resolves `GIT` to the same `git` binary, so no test can catch it. On case-sensitive Linux CI, `GIT` isn't found, the helper returns `False`, and the dirty-tree test kills it. The other mutations of that argument list — `status`, `--porcelain`, and the `--` separator (the last pinned by a dash-prefixed-filename test) — are all killed. |

If any of these stops being equivalent — e.g. a future code path reads `Tree` node metadata, making
row 2 observable — it resurfaces as a survivor and gets a real test.
