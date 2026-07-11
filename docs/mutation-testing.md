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

## Current result

**763 / 781 mutants killed — the remaining 18 are equivalent mutants** (changes that cannot alter
observable behavior, so no test *can* catch them; this is the well-known
[equivalent mutant problem](https://en.wikipedia.org/wiki/Mutation_testing#Equivalent_mutants)).
Rather than contort the suite to "kill" them — which would only pin implementation trivia — they are
enumerated and justified here. Every behavioral mutant mutmut generates is killed (see the scope note
below for what that set covers).

### Scope: what the 781 covers

mutmut 3.6 mutates **module-level functions only** — it does not generate mutants for class-method
bodies. So the 781 spans the package's 28 module-level functions (the operator catalog, spans, mutant
generation, the loop, JUnit parsing, the reporter, the CLI, the adapter), but **not** the method
bodies: `GdUnit4Runner.run`/`command`, `CommandRunner.run`, the two `replacements` implementations,
`MutationRun`'s properties, `Mutant.apply`, `Span.__post_init__`, and `SuiteResult.failed`/`passed`.
Those are covered by unit tests but are not *mutation-measured* here — so read the score as "every
behavioral
mutant mutmut generates is killed," over the module-level surface. Closing that gap with a
method-mutating tool (e.g. cosmic-ray) is tracked as follow-up.

### The 18 equivalent mutants

**1. `encoding="utf-8"` → `encoding=None` / omitted / `"UTF-8"`  (11 mutants)**
In `engine/loop.py` (`_run_one`, writing and restoring the mutated file) and `cli.py`
(`_load_gdscript` reading the source, `run_mutation` writing the JSON report).
- `"UTF-8"` is a codec *alias* of `"utf-8"` — byte-for-byte identical.
- `encoding=None` / omitting the argument falls back to the platform's default text encoding, which
  on a UTF-8 locale — the CI runner and every environment gdmutant is used in — is itself UTF-8, so
  the bytes are identical.

  No black-box test can distinguish these from `"utf-8"`. Specifying `encoding="utf-8"` explicitly is
  nonetheless correct — it is the guarantee that keeps the equivalence true across platforms, rather
  than relying on the ambient locale.

**2. `gather_metadata=True` → `False` / omitted  (3 mutants)**
In `adapters/gdscript/_parse`. `gather_metadata` attaches source spans to *Tree nodes*; the token
line/column positions the adapter actually reads are set by lark's lexer regardless of this flag
(verified directly). So toggling it does not change any value the adapter uses.

**3. Defensive `assert` `and` → `or`  (3 mutants)**
In `adapters/gdscript/_span_of`: `assert line and col and end_line and end_col`. This is a
type-narrowing guard for the `Optional[int]` token-position types. Because lark always populates
those positions (each is `>= 1`, i.e. truthy), every `and`/`or` re-association of always-truthy
operands evaluates identically — the guard never trips, so no test can observe a difference.

**4. `"git"` → `"GIT"` in the uncommitted-changes check  (1 mutant)**
In `cli._has_uncommitted_changes`, which shells out to `git status --porcelain`. On a
**case-insensitive filesystem** — the macOS default, where this dogfood runs — the OS resolves
`GIT` to the same `git` binary, so the mutant behaves identically and no test can catch it. On a
case-sensitive filesystem (Linux CI) `GIT` is not found, the helper returns `False`, and the
dirty-tree test kills it. So this is *environment-equivalent*, not universally so; the other
mutations of that argument list (`status`, `--porcelain`, and the `--` separator — the last pinned
by a dash-prefixed-filename test) are all killed.

If any of these stops being equivalent (e.g. a future code path reads `Tree` node metadata, making
mutant group 2 observable), it will resurface as a survivor and get a real test.
