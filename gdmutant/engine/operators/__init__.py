"""Language-neutral operator catalog.

An `Operator` produces, for a source token, the mutated token(s) to try — each becomes one
candidate mutant when an adapter finds that token in real source and applies the swap via
`engine.spans`. The catalog covers DESIGN.md FG-2.1's token-level mutations: comparison, boolean,
arithmetic, and constant (a boolean-literal flip **and** a numeric-literal bump). Beyond the FG-2.1
minimum it also covers constructs common in real code: compound assignment (`+=`↔`-=`, `*=`↔`/=`),
modulo (`%`→`*`/`/`), and unary-`not` removal (a token-level deletion).

Two shapes implement the `Operator` protocol:

* `TableOperator` — a fixed original→replacements lookup (comparison / boolean / arithmetic /
  the boolean-literal flip).
* `NumericBumpOperator` — a *computed* rule for integer literals. Integer literals are an infinite
  domain, so they can't be a static table — which is why the operator interface is a protocol, not
  a single table type (Slice 3's adapter binds against this shape).

Structural statement-deletion (also FG-2.1) removes an AST statement, not a token — it needs
statement-node handling rather than a token swap, so it lives in the GDScript adapter
(`_statement_deletions`), not this token catalog, with a generation-time guard against
Godot-uncompilable deletions. See docs/decisions/0002 and 0007.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Operator(Protocol):
    """A mutation operator: it names itself and yields replacement tokens for a source token."""

    @property
    def id(self) -> str:
        """A stable identifier for this operator (recorded on each mutant / in reports)."""
        ...

    def replacements(self, token: str) -> tuple[str, ...]:
        """Replacement tokens this operator produces for `token` (empty if it doesn't apply)."""
        ...


def applies(operator: Operator, token: str) -> bool:
    """True if `operator` produces at least one mutant for `token`."""
    return len(operator.replacements(token)) > 0


@dataclass(frozen=True)
class TableOperator:
    """An operator defined by a fixed original-token → replacement-tokens table."""

    id: str
    table: Mapping[str, tuple[str, ...]]

    def replacements(self, token: str) -> tuple[str, ...]:
        """`token`'s replacements per `table`, or empty if `token` isn't one of its keys."""
        return self.table.get(token, ())


@dataclass(frozen=True)
class NumericBumpOperator:
    """Bump an integer literal by ±1 — the FG-2.1 "bump an int literal" case.

    Integer literals are an infinite domain, so this is a computed rule, not a table. gdtoolkit
    folds a leading sign into the ``NUMBER`` token (``-5`` is one token, not ``-`` + ``5``), so
    negative literals are handled here too: ``n`` -> ``n+1``, ``n-1`` (e.g. ``5`` -> ``6``, ``4``;
    ``0`` -> ``1``, ``-1``; ``-5`` -> ``-4``, ``-6``). Every replacement is itself an integer
    literal the operator recognizes, so mutations are reversible. Only bare decimal integers;
    floats, hex, and digit-separator forms are left for a later slice.
    """

    id: str = "numeric"

    def replacements(self, token: str) -> tuple[str, ...]:
        """`token` bumped by +1 and -1 (as strings), or empty if `token` isn't a bare decimal
        integer literal, or is the non-canonical literal ``"-0"``."""
        digits = token[1:] if token.startswith("-") else token
        if not (digits.isascii() and digits.isdigit()):
            return ()
        n = int(token)
        if token.startswith("-") and n == 0:
            return ()  # "-0" isn't a real negative literal; skip it (keeps bumps reversible)
        return (str(n + 1), str(n - 1))


COMPARISON = TableOperator(
    "comparison",
    {
        ">": (">=",),
        ">=": (">",),
        "<": ("<=",),
        "<=": ("<",),
        "==": ("!=",),
        "!=": ("==",),
    },
)
BOOLEAN = TableOperator("boolean", {"and": ("or",), "or": ("and",)})
#: Arithmetic. GDScript overloads ``+`` for string concatenation, and its ``String`` defines no
#: ``-``, so the GDScript adapter's `find_sites` skips a ``+`` whose expression has a string-literal
#: operand — the same operand typing it applies to ``%`` (see `MODULO`). Catalogs carry no
#: operand-type information, so that judgment belongs to the adapter, not this table.
ARITHMETIC = TableOperator("arithmetic", {"+": ("-",), "-": ("+",), "*": ("/",), "/": ("*",)})
CONSTANT = TableOperator("constant", {"true": ("false",), "false": ("true",)})
NUMERIC = NumericBumpOperator()
#: Compound assignment — gdtoolkit tokenizes ``+=``/``-=``/``*=``/``/=`` atomically, so these are a
#: plain involutive swap. Without it, ``energy += speed`` produces **zero** mutants. GDScript
#: overloads ``+=`` for string appending and its ``String`` defines no ``-``, so the GDScript
#: adapter's `find_sites` skips a ``+=`` whose right operand is a string — the same operand typing
#: it applies to ``+`` (see `ARITHMETIC`) and ``%`` (see `MODULO`).
COMPOUND_ASSIGN = TableOperator(
    "compound-assign", {"+=": ("-=",), "-=": ("+=",), "*=": ("/=",), "/=": ("*=",)}
)
#: Modulo — swap ``%`` for ``*`` or ``/``. Directional on purpose: it targets the ``%`` the catalog
#: otherwise ignores, without adding a ``%`` mutant to every ``*``/``/`` in the codebase. GDScript
#: overloads ``%`` for string formatting, so the GDScript adapter's `find_sites` skips a ``%`` whose
#: left operand is a string literal (formatting, not modulo). Not involutive (see operator tests).
MODULO = TableOperator("modulo", {"%": ("*", "/")})
#: Unary ``not`` removal — deleting the keyword flips the guarded condition, a strong mutation.
#: Modelled as a swap to the empty string (a token-level deletion); the adapter's NF-5 re-parse
#: drops any result that doesn't parse. Not involutive: ``""`` is not a token to swap back.
LOGICAL_NOT = TableOperator("logical-not", {"not": ("",)})

#: The default catalog, in a stable order (new operators appended so existing mutant ids/order are
#: unchanged — NF-1). The intermediate list widens the heterogeneous operator types to the
#: `Operator` protocol before forming the tuple.
_CATALOG: list[Operator] = [
    COMPARISON,
    BOOLEAN,
    ARITHMETIC,
    CONSTANT,
    NUMERIC,
    COMPOUND_ASSIGN,
    MODULO,
    LOGICAL_NOT,
]
CATALOG: tuple[Operator, ...] = tuple(_CATALOG)


def all_replacements(token: str, catalog: tuple[Operator, ...] = CATALOG) -> list[tuple[str, str]]:
    """Every ``(operator_id, replacement)`` candidate the catalog produces for `token`."""
    return [(op.id, repl) for op in catalog for repl in op.replacements(token)]
