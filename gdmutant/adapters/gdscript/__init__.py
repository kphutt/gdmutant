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
    """``(line, name)`` for every bracketed operator name in an ignore directive that no operator in
    `catalog` produces — a likely typo, since such a name silently suppresses nothing. The CLI warns
    on these; the run is not failed (an unknown name is simply inert)."""
    valid = {op.id for op in catalog}
    return [
        (line, name)
        for line, directive in _ignore_directives(source).items()
        if directive.operators is not None
        for name in sorted(directive.operators)
        if name not in valid
    ]


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


def generate_mutants(
    path: str, source: str, catalog: tuple[Operator, ...] = CATALOG
) -> list[Mutant]:
    """All mutants for `source`, each tagged ``ignore_reason`` if a ``# gdmutant: ignore`` directive
    on its line applies to it; `path` is recorded on each mutant for reporting."""
    mutants = generate(path, find_sites(source, catalog), catalog)
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
