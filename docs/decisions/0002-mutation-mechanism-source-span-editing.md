---
type: decision
status: active
created: 2026-07-10
---

# Mutate by AST-guided source-span editing, not tree unparsing

## Status
Accepted

## Context
A mutation is "apply one operator at one AST node." Turning that into mutated *source* has two shapes:

- (a) Unparse the tree: mutate the gdtoolkit/lark parse tree and re-emit the whole file. But
  gdtoolkit has no general "unparse an arbitrary mutated tree" API, and its formatter *reformats*
  whole files, which would rewrite unrelated formatting and comments. That produces noisy,
  low-fidelity mutants where a survivor's diff is buried in reflow.
- (b) AST-guided source-span editing: use the tree only to *locate* the exact token span, then
  replace that span in the original source text.

A spike validated (b). `gdtoolkit.parser.parser.parse(code, gather_metadata=True)` yields precise,
1-indexed token positions (`line` / `column` / `end_line` / `end_column`) for operator tokens. For
`if a > b and b >= 0:`, the `>` reports L2 C7–C8, `and` L2 C11–C14, `>=` L2 C17–C19.

## Decision
Mutate by AST-guided source-span editing:
1. Parse with `gather_metadata=True` to locate the operator token's span (the adapter's job, because
   it knows the language's AST).
2. Replace exactly that span in the original source, a language-neutral text operation in
   `gdmutant/engine/spans.py`.
3. Re-parse the result to enforce NF-5: if the mutated source no longer parses, the mutant is
   classified *invalid* and never counted as *killed*. A wrong or invalid mutant that reads as
   "killed" would silently inflate the score, the worst failure mode for a mutation tester.

## Consequences
- A mutant differs from the original by exactly the operator span → clean, reviewable diffs, and a
  survivor points at one token. All surrounding formatting and comments are preserved untouched.
- Positions are the contract. The neutral span-replace utility is independent of any parser. Only the
  adapter's *locating* code depends on gdtoolkit's position semantics.
- Structural operators fit the same mechanism: statement deletion locates the statement's span and
  replaces it (e.g. with `pass`), still one span edit plus a re-parse guard.
- No dependency on a full-tree unparser, and no risk of reformat noise.
