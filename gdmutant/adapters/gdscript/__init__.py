"""GDScript adapter — the mutation half (no Godot).

Locates mutable tokens with gdtoolkit and turns them into engine `MutationSite`s, generates
`Mutant`s (via `engine.mutants.generate`), and enforces **NF-5** by re-parsing each mutant.

gdtoolkit does not surface tokens inside string literals or comments, and tokenizes compound
operators (`+=`, `->`, `>=`) atomically (verified with the tokenization spike), so keeping only
tokens the operator catalog mutates never edits inside a string/comment or half of a compound
operator. The Godot test runner is a separate concern (Slice 4).
"""

from __future__ import annotations

from gdtoolkit.parser import parser as _gdparser
from lark import Token, Tree
from lark.exceptions import LarkError

from gdmutant.engine.mutants import Mutant, MutationSite, generate
from gdmutant.engine.operators import CATALOG, Operator, all_replacements
from gdmutant.engine.spans import Span


def _parse(source: str) -> Tree[Token]:
    tree: Tree[Token] = _gdparser.parse(source, gather_metadata=True)
    return tree


def _span_of(tok: Token) -> Span:
    line, col, end_line, end_col = tok.line, tok.column, tok.end_line, tok.end_column
    # gather_metadata=True populates positions; guard defensively for the type-checker.
    assert line and col and end_line and end_col  # pragma: no cover
    return Span(line, col, end_line, end_col)


def find_sites(source: str, catalog: tuple[Operator, ...] = CATALOG) -> list[MutationSite]:
    """Every token in `source` that `catalog` can mutate, located via gdtoolkit.

    Filtering by "does the catalog mutate this value" is sufficient: gdtoolkit never surfaces
    tokens from inside string literals or comments, so this never edits within one. `catalog` is
    threaded through so site selection matches generation (a custom catalog finds its own sites).
    """
    return [
        MutationSite(tok.value, _span_of(tok))
        for tok in _parse(source).scan_values(lambda v: isinstance(v, Token))
        if all_replacements(tok.value, catalog)
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
