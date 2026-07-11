"""GDScript adapter — the mutation half (no Godot).

Locates mutable tokens with gdtoolkit and turns them into engine `MutationSite`s, generates
`Mutant`s (via `engine.mutants.generate`), and enforces **NF-5** by re-parsing each mutant.

gdtoolkit does not surface tokens inside string literals or comments, and tokenizes compound
operators (`+=`, `->`, `>=`) atomically (verified with the tokenization spike), so keeping only
tokens the operator catalog mutates never edits inside a string/comment or half of a compound
operator. The Godot test runner is a separate concern (Slice 4).
"""

from __future__ import annotations

from itertools import pairwise

from gdtoolkit.parser import parser as _gdparser
from lark import Token, Tree
from lark.exceptions import LarkError

from gdmutant.engine.mutants import Mutant, MutationSite, generate
from gdmutant.engine.operators import CATALOG, Operator, all_replacements
from gdmutant.engine.spans import Span


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


_IGNORE_MARKER = "# gdmutant: ignore"


def _ignored_lines(source: str) -> set[int]:
    """1-based line numbers carrying a ``# gdmutant: ignore`` marker — a ``# noqa``-style opt-out
    for lines whose mutants are equivalent (unkillable) or otherwise not worth reporting.

    Comments aren't tokens, so this scans the raw source. Lines are split on ``\\n`` only, matching
    the engine's line counting (spans.py), so the numbers line up with token positions.
    """
    return {i for i, line in enumerate(source.split("\n"), start=1) if _IGNORE_MARKER in line}


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
    Tokens on a *physical* line marked ``# gdmutant: ignore`` are skipped — line-scoped, like
    ``# noqa`` (a multi-line statement needs the marker on each line; see docs/decisions/0004).
    A ``%`` used as the string-format operator is skipped too (see `_string_format_percents`).
    """
    ignored = _ignored_lines(source)
    tree = _parse(source)
    format_percents = _string_format_percents(tree)
    return [
        MutationSite(tok.value, _span_of(tok))
        for tok in tree.scan_values(lambda v: isinstance(v, Token))
        if all_replacements(tok.value, catalog)
        and tok.line not in ignored
        and (tok.line, tok.column) not in format_percents
    ]


def generate_mutants(
    path: str, source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[Mutant]:
    """All mutants for `source`; `path` is recorded on each mutant for reporting."""
    return generate(path, find_sites(source, catalog), catalog)


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
