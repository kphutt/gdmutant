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

Structural statement-deletion (also FG-2.1) removes an AST statement, not a token, so it is applied
by the adapter (Slice 3), which has the parse tree. See docs/decisions/0002.
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
    """Bump a non-negative integer literal — the FG-2.1 "bump an int literal" case.

    Integer literals are an infinite domain, so this is a computed rule, not a table. Only bare
    decimal-digit tokens are handled (a leading sign is a separate token; floats, hex, and
    digit-separator forms are left for a later slice — they'd need their own token recognition).

    Replacements are always **non-negative bare integers** (`n+1`, and `n-1` when `n > 0`), so a
    mutant token is itself a plain literal the operator recognizes — the mutation stays a single
    token and is reversible, and it never introduces a sign token.
    """

    id: str = "numeric"

    def replacements(self, token: str) -> tuple[str, ...]:
        if not (token.isascii() and token.isdigit()):
            return ()
        n = int(token)
        if n == 0:
            return ("1",)
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
