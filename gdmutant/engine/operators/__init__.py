"""Language-neutral operator catalog.

An `Operator` produces, for a source token, the mutated token(s) to try — each becomes one
candidate mutant when an adapter finds that token in real source and applies the swap via
`engine.spans`. The catalog covers DESIGN.md FG-2.1's token-level mutations: comparison, boolean,
arithmetic, and constant (a boolean-literal flip **and** a numeric-literal bump).

Two shapes implement the `Operator` protocol:

* `TableOperator` — a fixed original→replacements lookup (comparison / boolean / arithmetic /
  the boolean-literal flip).
* `NumericBumpOperator` — a *computed* rule for integer literals. Integer literals are an infinite
  domain, so they can't be a static table — which is why the operator interface is a protocol, not
  a single table type (Slice 3's adapter binds against this shape).

Structural statement-deletion (also FG-2.1) removes an AST statement, not a token — it needs
statement-node handling rather than a token swap, so it lands in its own later slice (tracked
separately), not this token catalog. See docs/decisions/0002.
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
        digits = token[1:] if token.startswith("-") else token
        if not (digits.isascii() and digits.isdigit()):
            return ()
        n = int(token)
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
ARITHMETIC = TableOperator("arithmetic", {"+": ("-",), "-": ("+",), "*": ("/",), "/": ("*",)})
CONSTANT = TableOperator("constant", {"true": ("false",), "false": ("true",)})
NUMERIC = NumericBumpOperator()

#: The default catalog, in a stable order. The intermediate list widens the heterogeneous
#: operator types to the `Operator` protocol before forming the tuple.
_CATALOG: list[Operator] = [COMPARISON, BOOLEAN, ARITHMETIC, CONSTANT, NUMERIC]
CATALOG: tuple[Operator, ...] = tuple(_CATALOG)


def all_replacements(token: str, catalog: tuple[Operator, ...] = CATALOG) -> list[tuple[str, str]]:
    """Every ``(operator_id, replacement)`` candidate the catalog produces for `token`."""
    return [(op.id, repl) for op in catalog for repl in op.replacements(token)]
