---
type: explanation
status: active
created: 2026-07-11
---

# Mutation testing gdmutant itself

gdmutant is a mutation tester, so its own Python suite is held to the standard it exists to enforce:
a test suite should not just *cover* a line, it should *catch a bug* on that line. We dogfood this
with [mutmut](https://github.com/boxed/mutmut), which mutates `gdmutant/` and re-runs the suite
against each mutant. A surviving mutant is a change no test objected to: a gap.

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

Two ordinary-looking things a new test can do will abort that baseline, so mutmut evaluates zero
mutants and the score stops existing rather than going down. `tests/test_mutation_baseline_inputs.py`
catches both before a merge, on every platform (including Windows, where mutmut itself cannot run).

The first is reading a repository file that `also_copy` does not name. A test that reads a file at the repository root
passes a normal `pytest` run and raises `FileNotFoundError` inside the copied tree, which fails the
baseline, so mutmut aborts and evaluates zero mutants, and the score stops existing rather than
going down. Nothing about writing that test hints at it. `tests/test_mutation_baseline_inputs.py`
closes the loop: it reads every test module, works out which repository entries the suite reaches
for, and fails naming the one that is missing from `also_copy`. It is a plain test, so it runs in
`verify` on every platform (including Windows, where mutmut itself cannot run) before a merge
rather than after one. The second is changing the working directory. mutmut resolves `source_paths` (the relative path
`gdmutant`) against the live working directory every time a mutated module-level function runs, so a
test that chdirs into a temporary directory and then calls into `gdmutant` fails inside mutmut's own
trampoline. Point the code under test at an absolute path instead. For a `.gdmutant.toml` fixture,
set `cli._CONFIG_FILENAME` to the file rather than standing in its directory.

Neither scan is a proof: the first sees only paths built from the suite's usual
`Path(__file__).resolve().parent.parent` idiom, and the second only the `chdir` spellings it lists.
The zero-mutant check below is what catches everything else.

CI runs mutmut in report mode as a non-blocking job (not a required status check): it
surfaces the score on the run summary and never fails the build over survivors or a low score. But
a *baseline* failure (the suite not running cleanly unmutated, so mutmut evaluates zero mutants)
is a different thing entirely: that's a real defect, not an advisory signal, so it fails the job (and
the run) for real. See `.github/workflows/mutation.yml`'s header comment for why that split exists
and why the job deliberately has no job-level `continue-on-error`. It complements the coverage gate:
coverage says a line *ran*, mutation says a bug there would be *caught*.

A second, narrower mutation run happens locally: the manual pre-commit hook (`gdmutant-mutation`)
runs [poodle](https://github.com/WiredNerd/poodle), diff-scoped to files changed vs `origin/main`.
It's local rather than a second CI job partly because mutmut can't run on the maintainer's Windows
machine at all (`os.fork()`), and partly because it's a targeted, on-demand check rather than a
standing cost. See `docs/decisions/0013-windows-local-mutation-testing.md` for the full reasoning,
and `poodle.toml` / `scripts/check_mutation_baseline.py` for the config. The two tools will not
report identical scores on the same file (different operator sets). The local run's job is "did this
change just go untested," not "match CI's number."

## Current result

3,275 of 3,773 mutants killed: 86.8%, with 498 survivors, measured 2026-07-31 at commit `10fdb48`.
A mutation score is a snapshot: it is only true at the commit it was measured against, so read that
commit and date as part of the number, not as decoration. `main` has moved since. Re-measure rather
than trust this line past that commit.

The first real number this project had, right after the baseline was repaired, was 3,088 of 3,560
mutants killed (86.7%, 472 survivors), measured on CI. The figure above is a full local rebuild of
that same measurement, done later on a codebase that had grown in the meantime: mutmut stayed
pinned at 3.6.0 across both runs, so the larger mutant count is codebase growth, not the tool
widening its reach. The percentage barely moved, 86.7% to 86.8%.

Those 498 survivors are not triaged. The 18 below are equivalent mutants: changes that cannot
alter observable behavior, so no test *can* catch them (the well-known [equivalent mutant
problem](https://en.wikipedia.org/wiki/Mutation_testing#Equivalent_mutants)), and they are still
equivalent. But they were enumerated against a much smaller codebase, over a run whose own mutant
total is withdrawn as unreproducible ([ADR-0008's
correction](decisions/0008-method-body-mutation-manual-spotcheck.md#correction-2026-07-31)), and
they do not account for the rest. Working through the remaining survivors, and deciding which are
real gaps and which are equivalents, is outstanding work. Until it is done, read the 86.8% as a
measurement and not as a claim that every behavioral mutant is killed.

### Scope: the method bodies this run never reaches

Two claims get run together here, and only the first one is about mutmut.

The first: mutmut 3.6.0 is not module-level-only. It walks into a `ClassDef` and builds a trampoline
for each method it finds (`mutmut/mutation/file_mutation.py`), so an ordinary class method is
mutated like any other function. What it refuses is a *decorated* class or function, with
`@staticmethod` and `@classmethod` the only exemptions, because copying a decorated definition for
the trampoline can re-run the decorator. An earlier version of this page said mutmut generated
nothing inside method bodies at all, which was never right. See [ADR-0008's
correction](decisions/0008-method-body-mutation-manual-spotcheck.md#correction-2026-07-31), which
also records that mutmut has been pinned at 3.6.0 for every run this project has ever measured.

The second: whether the run therefore spans this package. It does not. gdmutant is built almost
entirely out of frozen dataclasses, so nearly every class here carries a decorator and nearly every
method body here is skipped. Measured on 2026-07-31, mutmut generates 3,773 mutants across
`gdmutant/` and none of them lands inside a class-method body.

That is 425 lines mutmut will not enter, about 12% of every line inside a function or method
definition in the package. (Counted as the physical lines from each `def` to the end of its
definition, which is the count that reproduces the per-file figures below.) It is also the wrong
12%:

| lines skipped | file | what is in them |
|---|---|---|
| 244 | `adapters/gdscript/runner.py` | the whole GdUnit4 and GUT runner: Godot command construction, JUnit XML parsing, the addon-prepare step. The file holds three `@dataclass` classes and no module-level function, so none of its 413 lines yields a mutant. |
| 94 | `engine/loop.py` | `MutationRun`'s ten counters and the mutation score, plus `_Progress` |
| 36 | `engine/runner.py` | `CommandRunner.run`, `SuiteResult.passed` / `.failed`, and the runner protocols' method stubs |
| 28 | `engine/mutants.py` | `Mutant.apply`, `Mutant.describe_change` |
| 16 | `engine/operators/__init__.py` | `TableOperator.replacements`, `NumericBumpOperator.replacements` |
| 7 | `engine/spans.py` | `Span.__post_init__` |

The mutation-operator catalog that `AGENTS.md` names as a sensitive path produces three mutants in
the whole run. The tables that define what a mutation *is* are all but unmeasured, and so is every
line that decides whether a test suite passed. A score computed over the rest of the package says
nothing about either.

This is the gap [`docs/decisions/0013`](decisions/0013-windows-local-mutation-testing.md) reaches
for a second, local tool to cover. Both that record and `0008` reached the right conclusion from a
wrong explanation, and both now carry a correction saying so.

### The 18 known equivalent mutants

| # | Mutation | Where | Why no black-box test can catch it |
|---|---|---|---|
| 11 | `encoding="utf-8"` → `None` / omitted / `"UTF-8"` | `engine/loop.py` `_run_one` (writing + restoring the mutated file), `cli.py` `_load_gdscript` (reading source), `run_mutation` (writing the JSON report) | `"UTF-8"` is a codec *alias* of `"utf-8"` (byte-identical). `None` / omitted falls back to the platform default text encoding, which on a UTF-8 locale (the CI runner and every environment gdmutant is used in) is itself UTF-8. Same bytes either way. The explicit `encoding="utf-8"` is still correct: it *guarantees* the equivalence across platforms instead of relying on the ambient locale. |
| 3 | `gather_metadata=True` → `False` / omitted | `adapters/gdscript/_parse` | `gather_metadata` attaches source spans to *Tree nodes*. The token line/column positions the adapter reads are set by lark's lexer regardless of the flag (verified directly). Toggling it changes no value the adapter uses. |
| 3 | defensive `assert` `and` → `or` | `adapters/gdscript/_span_of`: `assert line and col and end_line and end_col` | A type-narrowing guard for the `Optional[int]` token positions. lark always populates them (each `>= 1`, i.e. truthy), so every `and`/`or` re-association of always-truthy operands evaluates identically: the guard never trips, and no test can observe a difference. |
| 1 | `"git"` → `"GIT"` | `cli._git_backup` (shells out to `git status --porcelain`) | *Environment*-equivalent, not universal. On a case-insensitive filesystem (the macOS default, where this dogfood runs) the OS resolves `GIT` to the same `git` binary, so no test can catch it. On case-sensitive Linux CI, `GIT` isn't found, the helper reports that git could not be run (`backed_up=None`) rather than that the file is dirty, and the dirty-tree test kills it. The other mutations of that argument list (`status`, `--porcelain`, and the `--` separator, the last pinned by a dash-prefixed-filename test) are all killed. |

If any of these stops being equivalent (e.g. a future code path reads `Tree` node metadata, making
row 2 observable), it resurfaces as a survivor and gets a real test.
