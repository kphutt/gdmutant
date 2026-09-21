---
type: decision
status: active
created: 2026-09-20
---

# Keep the whole-file validity re-parse for now, and if it is ever made local, resume the real parse and prove it rejoins

## Status
Accepted: do not build it yet. The design below is the one to build if the trigger in
[When to revisit](#when-to-revisit) is ever met.

## Context
NF-5 in [`DESIGN.md`](../design/DESIGN.md) says gdmutant must never hand the test runner a file
that does not parse. `apply_mutant` in `gdmutant/adapters/gdscript/__init__.py` enforces that by
re-parsing the whole mutated file with gdtoolkit for every mutant. A mutant whose file fails is
`invalid`: never run, never scored. The two ways a faster check can be wrong are not equal.

- A false reject (a valid mutant called invalid) only hides a mutant from the report.
- A false accept (a broken mutant called valid) is the dangerous one. The broken file runs, Godot
  fails to load it, the tests fail, and the report counts the mutant as killed. The score goes up
  for a test that caught nothing, which is exactly what NF-5 exists to stop.

That re-parse was about 92% of gdmutant's own engine time. Skipping position data it never used
made it about 22% faster (see `CHANGELOG.md`). What is left grows with file size, because every
mutant pays for parsing every line of its file, not just the line it changed. The idea this ADR
weighs: parse only the top-level declaration (usually one function) that holds the mutant, so the
cost per mutant stops depending on the file's length.

### How gdtoolkit parses, and why "just parse the function" is not obviously safe

gdtoolkit uses lark, a Python parsing library, in its LALR mode. LALR is a table-driven parser
that reads tokens left to right and keeps a stack of numbered states. Three details matter here.

1. Tokens come from a contextual lexer. The lexer (the part that cuts text into tokens) asks the
   parser which state it is in and only considers the token kinds that state allows. So the same
   text can be cut into different tokens depending on what came before it. This is not
   theoretical: the prototype found that right after a top-level function ends, the parser is in
   a state that allows 89 token kinds, while right after a top-level `var` or `const` it is in a
   state that allows 17.
2. Indentation is turned into INDENT and DEDENT tokens by a post-lexer, gdtoolkit's
   `GDScriptIndenter`. It tracks bracket depth and an indentation stack, and inside brackets it
   looks back over earlier tokens to decide whether a multi-line lambda has started.
3. Some tokens can swallow a line break. The newline token also swallows following blank lines
   and comments. Strings in triple quotes run across lines. A backslash at the end of a line joins
   it to the next one.

So a function that parses on its own can still break the file. The gate test already pins the
obvious cases (`_CROSS_DECLARATION` in `tests/test_validity_gate.py`): a bracket or a string
opened in one function and never closed, and a body line pushed out to column 1. There are
subtler ones: a trailing backslash on a function's last line joins it to the next declaration's
first line, and a declaration that now starts with a space, a tab or `#` merges into the previous
declaration's newline token.

### What was measured

A scratch prototype (not committed) tried three designs against the reference answer, a
whole-file parse with position data, which is what the gate did before any speed work. It ran
over 399 real GDScript files, 55,451 lines in all: a GUT checkout, a private game project, and
this repo's `corpus/`. Those files give 35,466 catalog mutants, of which exactly one is invalid
(the known `-float(x)` to `+float(x)`). Because real code almost never makes invalid mutants, a
second sweep broke every file on purpose: at six sampled mutation sites per file, plus the first
and last character of each top-level declaration, it applied 27 deliberately bad replacements
(open and close brackets, all four quote forms, a backslash, a newline, `func`, `#`, a deletion,
and so on). That gave 63,126 cases, 50,581 of them invalid under the reference.

Timings in the sweeps ran 12 processes at once on a shared machine, so read them as ratios. The
single-process timings on `scripts/benchmark.py`'s synthetic files alternate the arms and keep
the best of three rounds.

| Design | Disagreements with the reference (catalog / broken) | Fell back to a whole-file parse (catalog) | Parse time vs whole file (catalog) |
|---|---|---|---|
| A. Parse the declaration on its own, behind a guard | 0 / 0 | 0.65% | 7.3x faster |
| B. Resume the real parse at the declaration, stop when it rejoins | 0 / 0 | none needed, see below | 7.5x faster |
| C. Re-lex the whole file, parse only the declaration | not built | not applicable | at most about 1.6x to 2.5x faster, see below |

Cost per mutant, synthetic benchmark files, single process:

| Functions | Lines | Whole file | Design A | Design B |
|---|---|---|---|---|
| 1 | 16 | 1.21 ms | 1.21 ms | 1.25 ms |
| 2 | 31 | 2.56 ms | 1.29 ms | 1.37 ms |
| 8 | 121 | 12.38 ms | 1.86 ms | 1.63 ms |
| 16 | 241 | 20.96 ms | 1.69 ms | 1.42 ms |
| 64 | 961 | 90.32 ms | 2.62 ms | 1.36 ms |

Design B's cost stays flat at about 1.4 ms whatever the file size, which is the whole point. The
one-time setup per file was 4 to 204 ms, about two whole-file parses of the original, paid once
per file rather than once per mutant.

Lexing alone, measured with lark's own lexer on four files, cost between 40% and 105% of a full
parse (62% on the 16-function synthetic file, 40% on a 2,923-line real file). That is what rules
out design C. It keeps most of the cost it set out to remove.

### What it is worth in a real run

The numbers above are gdmutant's own engine time, which is what `scripts/benchmark.py` measures
with a fake runner. A real run is dominated by Godot. The one real run recorded in `CHANGELOG.md`
reports an 18-mutant run on the corpus that took 6m 32s, with a 1.4 s baseline suite, and
4m 0s of it (about 61%) spent on 8 mutants that hit the timeout. Against that:

- On the corpus (files of 73 lines or fewer), the whole-file parse is a few milliseconds per
  mutant against at least 1.4 s of Godot per mutant. Removing all of it saves well under 1%.
- On a 16-function file, it saves about 19.5 ms per mutant against roughly 1.5 to 2 s of Godot,
  about 1%.
- The largest real file measured (2,923 lines) takes 109 ms to parse. Removing nearly all of
  that saves a few percent of a run only when the suite itself is fast.

So in a real run the ceiling is a few percent, reached only on files thousands of lines long. The
risk sits on the NF-5 gate, the one check whose failure silently inflates every score.

## Decision
Do not build it now. The gain in a real run is a few percent at most, the change touches the
gate whose failure is invisible, and the safe design (B) reaches into lark's internals, which ties
gdmutant to the exact lark and gdtoolkit versions in `uv.lock`. Timeouts, not parsing, are where
real runs spend their time.

If it is ever built, build design B, not A or C.

### Design B: resume the real parse, stop only when it provably rejoins

Once per file, parse the original with lark's interactive parser (its public API for stepping a
parse one token at a time) and record, at the first token of every top-level declaration, the
parser's state stack. A top-level declaration is a child of the parse tree's root, and its
boundary is the start of the line it begins on. Record a boundary only where the indenter is
clean there: bracket depth 0, indentation stack at column 0, no multi-line lambda open.

For each mutant:

1. Start from the saved state at the start of the declaration that holds the mutation, with a
   fresh lexer on the mutated text at that offset. This skips re-parsing everything before it.
2. Feed tokens exactly as the whole-file parse would. It is the same lexer, the same indenter
   and the same parse tables, on the same text.
3. At each later declaration boundary (shifted by however many characters the replacement added
   or removed), compare the live state stack and the indenter's state with the ones recorded for
   the original at that boundary. If they are equal, and the next token starts exactly at that
   boundary, stop and answer valid. From that point the parser is in the same state, reading
   the same text, so it will do exactly what it did on the original, which parsed.
4. If they never match, keep going to the end of the file. That is the whole-file parse, so its
   answer is the reference answer by definition.
5. A parse error anywhere is a reject, and it is the same error the whole-file parse would hit.

This is the known technique of incremental LR parsing with state matching (Wagner and Graham,
"Efficient and flexible incremental parsing", ACM TOPLAS 1998). It is not a new proof about
GDScript's grammar. The design never decides validity by reasoning about what a declaration can
or cannot affect. It checks the one fact that makes the rest of the file irrelevant: the parser is
back in the exact state it was in on the original.

There is no separate fallback path, because not rejoining is not a failure. It just means the
parse runs to the end. In the prototype 85.6% of catalog mutants rejoined at the next
declaration, and every other one was in the file's last declaration, where the parse reaches the
end of the file almost at once. None failed to rejoin. In the broken sweep 121 of 63,126 cases
never rejoined and paid a normal parse of the rest of the file.

One guard remains, at the start. If the mutated declaration now begins with a space, a tab, `#`
or a line break, that character would have extended the previous declaration's newline token,
so the recorded state is not a clean starting point. Then resume one declaration earlier, or
from the top of the file.

### Why not design A

Design A parses the mutated declaration on its own and trusts a local "valid" only behind a
guard: the replacement holds no newline or backslash, the declaration does not start with
whitespace or `#`, and its last line does not end in a backslash. Otherwise, or on a local
failure, it parses the whole file. It made no mistake in either sweep, and it is simpler. But
its safety rests on a hand-written argument about lark and gdtoolkit internals: that a
declaration which parses alone, followed by text that parsed before, still parses as a whole.
The contextual lexer breaks the simple form of that argument, because the tokens of the next
declaration depend on the state the mutated one ends in, and that state can differ (89 token
kinds after a function, 17 after a variable). No counterexample turned up, but the guard does
not check that state, so it rests on nobody having found one. Design B checks it directly, is
as fast or faster, and needs no fallback path.

### Why not design C

Re-lexing the whole file and parsing only the declaration would fix the lexer's view of the
boundaries, but lexing alone costs 40% to 105% of a whole parse. At best that is a 1.6x to 2.5x
gain, nothing at all on small files, and the parse-state question from design A remains.

### Failure modes considered

| Failure mode | How design B handles it |
|---|---|
| Bracket opened in the declaration and never closed | The parse keeps going past the boundary with the bracket open. The indenter check fails at every later boundary, so it runs to the end and fails where the whole-file parse fails. |
| String or triple-quoted string opened and never closed, or closed by a later declaration | The lexer swallows text past the boundary, so no token starts exactly at a boundary. No rejoin, parse runs to the end. Reference answer either way. |
| Body line pushed to column 1, or a new top-level statement appears | The state stack at the next boundary differs from the original, so no rejoin, parse runs on. |
| Trailing backslash joins the declaration to the next one | The next declaration's first token no longer starts at the boundary. No rejoin. |
| Declaration now starts with whitespace, `#` or a newline | The start guard resumes one declaration earlier. |
| Mutation changes which kind of statement ends the declaration, so the contextual lexer sees a different state | Only an exact state-stack match counts as rejoining. A different state never rejoins. |
| Indenter state differs at the boundary (bracket depth, indentation stack, open lambda) | Part of the rejoin check, compared explicitly, not assumed. |
| The indenter's lambda lookback reaches back before the resume point | The lookback runs only inside brackets, and bracket depth is 0 at the resume point, so the bracket it walks back to opened after it. Past that bracket it accepts only names and newlines, and a declaration starts with a keyword. A regression here would show up as a disagreement in the sweeps below. |
| Class-level `var` with an indented property body, inner classes, annotations on their own line, `enum Name` with its `{` on the next line, two statements on one line with `;` | Boundaries come from the parse tree, not from "lines at column 1", and are recorded only where the indenter is clean. A statement that spans a column-1 line is never split. |
| Mutation in the file's last declaration, file without a final newline, CRLF line endings | Nothing to rejoin, so the parse runs to the end of the file, the same text the whole-file parse reads. |
| A lark or gdtoolkit upgrade renames or changes an internal the fast path uses | See the evidence list: an import-time shape check and the two-sided gate test must fail loudly, never fall back quietly. |
| The fast path silently never runs, so a green test proves nothing about it | The gate test must count rejoins and fail if there were none (the "gate that checks nothing" shape in `AGENTS.md`). |

## Evidence the implementation PR must bring

1. `tests/test_validity_gate.py` extended, and green before the new code is switched on. Add
   `_CROSS_DECLARATION` fixtures for: a trailing backslash on a declaration's last line, a
   triple-quoted string opened in one function and closed by a later one, a declaration whose
   first character becomes a space, a tab or `#`, an annotation on its own line above a function,
   a class-level `var` with a `get:`/`set:` body, a mutation inside an inner class's method, a
   multi-line lambda in a class-level `var`, `enum Name` with its `{` on the next line, two
   statements joined by `;`, a CRLF file, a file with no final newline, and a replacement that
   contains a newline (the catalog never makes one, but the fast path must not assume that). Keep
   the rule that both answers appear. Some of these fixtures must be accepted by the reference,
   not only rejected, so a fast path that wrongly rejected them would also fail.
2. The same test asserts the fast path actually rejoined early on a known share of rows, so a
   fast path that quietly always runs to the end of the file cannot pass as covered.
3. A real-code sweep of every catalog mutant (not sampled) over at least the same three sources,
   with zero disagreements against the pinned reference, and the rejoin rate reported.
4. A reject-side sweep at real-code scale: every distinct mutation site, not a sample, with the
   27 broken replacements plus the first and last character of every declaration. Report how
   many cases the reference rejected, which must be most of them, and zero disagreements. The
   prototype's sampled version is the floor, not the bar.
5. A check that runs at import and fails loudly if the lark or gdtoolkit internals the fast path
   touches have changed shape. Candidates: lark's parser state, lexer state and line counter
   classes, the post-lexer connector, and the indenter's bracket depth, indentation stack and
   lambda bookkeeping. Any unexpected exception inside the fast path must fail the run, not
   quietly fall back to a whole-file parse. A quiet fallback would hide a broken fast path
   forever.
6. Benchmark numbers from `scripts/benchmark.py`, before and after, alternated on a quiet
   machine, and one real run against Godot showing the change in wall-clock time. The real run
   is the one that justifies the change.
7. The new logic mutation-tested with gdmutant's own self-mutation setup
   (`docs/mutation-testing.md`), survivors driven to zero or justified.

## When to revisit
Revisit when gdmutant's own time becomes a real share of a real run. The concrete signal is a
real Godot run where engine time per mutant is more than about 10% of the run's time per mutant.
That could happen if a future runner keeps Godot alive between mutants, or runs only the tests
that cover each mutant, so Godot's share per mutant drops. It could also happen if users bring
files thousands of lines long. Until then, the time is better spent on timeouts.

## Consequences
- The gate stays a plain whole-file parse: simple, obviously the reference, and tied to no lark
  internals.
- Engine time per mutant keeps growing with file size. The benchmark will keep showing that
  climb across `--sizes`, and that is now a known, accepted cost rather than a bug.
- `tests/test_validity_gate.py` stays as it is. It already guards the gate against any future
  speed work, and the fixture list above is ready for whoever picks this up.
