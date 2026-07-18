"""GDScript adapter — the mutation half (no Godot).

Locates mutable tokens with gdtoolkit and turns them into engine `MutationSite`s, generates
`Mutant`s (via `engine.mutants.generate`), and enforces **NF-5** by re-parsing each mutant.

gdtoolkit does not surface tokens inside string literals or comments, and tokenizes compound
operators (`+=`, `->`, `>=`) atomically (verified with the tokenization spike), so keeping only
tokens the operator catalog mutates never edits inside a string/comment or half of a compound
operator. The Godot test runner is a separate concern (Slice 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import pairwise

from gdtoolkit.parser import parser as _gdparser
from lark import Token, Tree
from lark.exceptions import LarkError

from gdmutant.engine.mutants import Mutant, MutationSite, generate
from gdmutant.engine.operators import CATALOG, Operator, all_replacements
from gdmutant.engine.spans import Span, text_at


def _parse(source: str) -> Tree[Token]:
    # gather_metadata attaches spans to Tree *nodes*; the token line/column positions this adapter
    # reads come from lark's lexer regardless. Kept on for any future tree-level use (harmless).
    tree: Tree[Token] = _gdparser.parse(source, gather_metadata=True)
    return tree


def _span_of(tok: Token) -> Span:
    line, col, end_line, end_col = tok.line, tok.column, tok.end_line, tok.end_column
    # lark's lexer always sets token positions; assert non-None only to satisfy the Optional types.
    assert line and col and end_line and end_col  # pragma: no cover
    return Span(line, col, end_line, end_col)


# The canonical annotation prefix (the spelling used in docs); the regex below is the lenient parse.
_IGNORE_MARKER = "# gdmutant: ignore"

# ``# gdmutant: ignore`` [optional ``[op1, op2]``] [optional reason]. Bare (no brackets) suppresses
# every operator on the line; ``[ops]`` suppresses only those; trailing text is the reason.
_IGNORE_RE = re.compile(r"#\s*gdmutant:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)$")


@dataclass(frozen=True)
class _IgnoreDirective:
    """A parsed ``# gdmutant: ignore`` annotation. `operators` is ``None`` for a bare marker (all
    operators on the line) or the set of operator ids to suppress; `reason` is the trailing text."""

    operators: frozenset[str] | None
    reason: str


def _ignore_directives(source: str) -> dict[int, _IgnoreDirective]:
    """1-based line -> the ``# gdmutant: ignore`` directive on it (a ``# noqa``-style opt-out for
    equivalent/unkillable mutants). Comments aren't tokens, so this scans raw source; lines split on
    ``\\n`` only, matching the engine's line counting (spans.py), so numbers align with tokens.

    ``# gdmutant: ignore`` → all operators on the line; ``ignore[comparison, numeric]`` → only them;
    text after the marker/brackets is the human reason (surfaced as the report's ``statusReason``).
    """
    directives: dict[int, _IgnoreDirective] = {}
    for i, line in enumerate(source.split("\n"), start=1):
        match = _IGNORE_RE.search(line)
        if match is None:
            continue
        ops_group, reason = match.group(1), match.group(2).strip()
        operators = (
            None
            if ops_group is None
            else frozenset(name.strip() for name in ops_group.split(",") if name.strip())
        )
        directives[i] = _IgnoreDirective(operators, reason)
    return directives


def unknown_ignore_operators(
    source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[tuple[int, str]]:
    """``(line, name)`` for every malformed operator scope in an ignore directive — either a name no
    operator in `catalog` produces (a likely typo) or **empty brackets** ``ignore[]`` (reported with
    ``name == ""``). Both silently suppress nothing, so the CLI warns; the run is never failed."""
    valid = {op.id for op in catalog}
    warnings: list[tuple[int, str]] = []
    for line, directive in _ignore_directives(source).items():
        if directive.operators is None:
            continue  # a bare marker (no brackets) is well-formed — suppresses the whole line
        if not directive.operators:
            warnings.append((line, ""))  # `ignore[]`: empty brackets, matches no operator
            continue
        warnings.extend((line, name) for name in sorted(directive.operators) if name not in valid)
    return warnings


def _string_format_percents(tree: Tree[Token]) -> set[tuple[int | None, int | None]]:
    """Positions ``(line, column)`` of ``%`` tokens that are the **string-format** operator, which
    the modulo operator must not mutate.

    ``%`` is overloaded in GDScript: arithmetic modulo *and* string formatting (``"fmt" % args``).
    The distinction is the direct **left operand**: a bare string literal means formatting. That has
    to be read from the parse tree, not the flat token stream — ``d["k"] % x`` is genuine modulo,
    but its token immediately before ``%`` is the *index* string ``"k"`` (its left operand is the
    ``d[...]`` ``subscr_expr`` subtree, not a string), so a token-adjacency check would wrongly drop
    it. Here each ``%`` in an ``mdr_expr`` (mul/div/remainder) node is skipped only when the node
    child *directly* to its left is a ``string`` node.

    Scope (deliberate): only a **bare string-literal** left operand is recognised. A *computed*
    string — concatenation like ``("Hi " + name) % x`` — is not, so its ``%`` is still mutated as
    modulo. Distinguishing that needs type inference (``(a + "b") % x`` is formatting or a type
    error depending on ``a``'s runtime type), which is out of scope for v0.1. This is the *noise*
    direction (a format ``%`` mutated to ``*``/``/`` errors at runtime — an ERROR verdict, never a
    silently-wrong survivor), unlike dropping a genuine modulo site. Tracked as a follow-up.
    """
    skip: set[tuple[int | None, int | None]] = set()
    for node in tree.iter_subtrees():
        if node.data != "mdr_expr":
            continue
        for prev, cur in pairwise(node.children):
            if (
                isinstance(cur, Token)
                and cur.value == "%"
                and isinstance(prev, Tree)
                and prev.data == "string"
            ):
                skip.add((cur.line, cur.column))
    return skip


def find_sites(source: str, catalog: tuple[Operator, ...] = CATALOG) -> list[MutationSite]:
    """Every token in `source` that `catalog` can mutate, located via gdtoolkit.

    Filtering by "does the catalog mutate this value" is sufficient: gdtoolkit never surfaces
    tokens from inside string literals or comments, so this never edits within one. `catalog` is
    threaded through so site selection matches generation (a custom catalog finds its own sites).
    A ``%`` used as the string-format operator is skipped (see `_string_format_percents`).

    ``# gdmutant: ignore`` annotations are **not** filtered here: a suppressed mutant is still
    *generated*, then marked ``ignore_reason`` in `generate_mutants` so it surfaces in the report as
    ``Ignored`` (excluded from the score) rather than vanishing (see docs/decisions/0004, 0006).
    """
    tree = _parse(source)
    format_percents = _string_format_percents(tree)
    return [
        MutationSite(tok.value, _span_of(tok))
        for tok in tree.scan_values(lambda v: isinstance(v, Token))
        if all_replacements(tok.value, catalog) and (tok.line, tok.column) not in format_percents
    ]


def _mark_ignored(mutant: Mutant, directives: dict[int, _IgnoreDirective]) -> Mutant:
    """Return `mutant` tagged with an `ignore_reason` if an ignore directive on its line applies to
    its operator (a bare directive applies to every operator; ``[ops]`` only to the named ones)."""
    directive = directives.get(mutant.span.line)
    if directive is None:
        return mutant
    if directive.operators is not None and mutant.operator_id not in directive.operators:
        return mutant
    return replace(mutant, ignore_reason=directive.reason)


# Statement-deletion (FG-2.1) — replace a statement with ``pass``. Structural, not a token swap, so
# it's a separate path from the token catalog (docs/decisions/0007). Deletes expression statements
# (calls, assignments, ``+=``) and ``return``s. Declarations (``func_var_stmt``) are deferred:
# deleting one either breaks a later reference or is equivalent (unused) — both noise.
_DELETABLE_STMT_NODES = frozenset({"expr_stmt", "return_stmt"})
_FUNCTION_SCOPE_NODES = frozenset({"func_def", "lambda"})
_SCOPE_HEADER_NODES = frozenset({"func_header", "lambda_header"})
STATEMENT_DELETION_ID = "statement-deletion"
_STATEMENT_REPLACEMENT = "pass"


def _scope_requires_a_return_value(scope: Tree[Token]) -> bool:
    """True if `scope` (a ``func_def`` or ``lambda``) declares a **non-void** return type — so Godot
    requires every path to return a value, and deleting a ``return`` may make it not compile.

    A ``lambda_header`` carries the same optional ``TYPE_HINT`` token as a ``func_header``, so a
    *typed lambda* (``func() -> int: return 9``) is guarded exactly like a typed function: deleting
    its return is the same "not all code paths return a value" Godot error (verified via
    ``--check-only``). An untyped lambda or ``-> void`` scope has no such requirement.
    """
    header = next(
        (c for c in scope.children if isinstance(c, Tree) and c.data in _SCOPE_HEADER_NODES), None
    )
    return header is not None and any(
        isinstance(c, Token) and c.type == "TYPE_HINT" and c.value != "void"
        for c in header.children
    )


def _scope_deletable_statements(scope: Tree[Token]) -> list[Tree[Token]]:
    """Deletable statement nodes inside `scope`, descending through control-flow blocks but NOT into
    nested function scopes (a lambda's statements belong to the lambda, its own scope)."""
    found: list[Tree[Token]] = []

    def walk(node: Tree[Token]) -> None:
        for child in node.children:
            if not isinstance(child, Tree) or child.data in _FUNCTION_SCOPE_NODES:
                continue
            if child.data in _DELETABLE_STMT_NODES:
                found.append(child)
            else:
                walk(child)

    walk(scope)
    return found


def _statement_deletions(path: str, source: str) -> list[Mutant]:
    """One ``pass``-replacement mutant per deletable single-line statement (FG-2.1).

    A ``return`` is emitted only when deleting it can't break compilation (docs/decisions/0007): the
    enclosing function is untyped/``void``, **or** the function body's last top-level statement is a
    *different* ``return`` (a guaranteed final return backstops the deletion, so every path still
    returns a value). gdtoolkit has no return-path analysis, so this generation-time guard — not
    NF-5's re-parse — is what keeps deletion mutants loadable in Godot (same pattern as
    `_string_format_percents`). Multi-line statements are skipped (`spans.py` single-line). Mutants
    are returned in document order.
    """
    mutants: list[Mutant] = []
    for scope in _parse(source).iter_subtrees():
        if scope.data not in _FUNCTION_SCOPE_NODES:
            continue
        typed = _scope_requires_a_return_value(scope)
        body = [
            c for c in scope.children if isinstance(c, Tree) and c.data not in _SCOPE_HEADER_NODES
        ]
        last = body[-1] if body else None
        last_is_return = last is not None and last.data == "return_stmt"
        for stmt in _scope_deletable_statements(scope):
            if stmt.data == "return_stmt" and typed and not (last_is_return and stmt is not last):
                continue  # deleting it would leave a typed function with no guaranteed return value
            meta = stmt.meta
            if meta.empty or meta.line != meta.end_line:
                continue  # no span, or a multi-line statement (spans.py edits a single line only)
            span = Span(meta.line, meta.column, meta.end_line, meta.end_column)
            original = text_at(source, span)
            mutants.append(
                Mutant(path, span, STATEMENT_DELETION_ID, original, _STATEMENT_REPLACEMENT)
            )
    return sorted(mutants, key=lambda m: (m.span.line, m.span.column))


def generate_mutants(
    path: str, source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[Mutant]:
    """All mutants for `source`, each tagged ``ignore_reason`` if a ``# gdmutant: ignore`` directive
    on its line applies to it; `path` is recorded on each mutant for reporting.

    Token-swap mutants (catalog) come first, then statement-deletion mutants (appended so existing
    mutant ids/order are unchanged — NF-1)."""
    mutants = generate(path, find_sites(source, catalog), catalog) + _statement_deletions(
        path, source
    )
    directives = _ignore_directives(source)
    if not directives:
        return mutants
    return [_mark_ignored(m, directives) for m in mutants]


def is_valid_gdscript(source: str) -> bool:
    """True if `source` parses as GDScript — the NF-5 gate."""
    try:
        _parse(source)
    except LarkError:
        return False
    return True


def apply_mutant(mutant: Mutant, source: str) -> tuple[str, bool]:
    """Apply `mutant` to `source`; return ``(mutated_source, is_valid)``.

    `is_valid` is False when the mutant produces unparseable GDScript, so the engine classifies it
    as invalid and never counts it as "killed" (NF-5).
    """
    mutated = mutant.apply(source)
    return mutated, is_valid_gdscript(mutated)
