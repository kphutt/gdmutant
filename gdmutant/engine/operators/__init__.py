"""Language-neutral operator catalog.

An `Operator` is a named set of **token substitutions**: for a source token it yields the mutated
token(s) to try — each yielded token becomes one candidate mutant when an adapter finds that token
in real source and applies the swap via `engine.spans`. Comparison / boolean / arithmetic /
constant swaps live here (DESIGN.md FG-2.1).

Structural operators (statement deletion, FG-2.1) are *not* token swaps — they remove an AST
statement — so they are applied by the adapter, which has the parse tree; they are not in this
token catalog. See docs/decisions/0002.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Operator:
    """A named mutation operator defined by a token-substitution table.

    `table` maps an original token to the tuple of replacement tokens the operator produces for
    it. A token absent from the table means the operator does not apply.
    """

    id: str
    table: Mapping[str, tuple[str, ...]]

    def replacements(self, token: str) -> tuple[str, ...]:
        """Replacement tokens this operator produces for `token` (empty if it doesn't apply)."""
        return self.table.get(token, ())

    def applies_to(self, token: str) -> bool:
        return token in self.table


COMPARISON = Operator(
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
BOOLEAN = Operator("boolean", {"and": ("or",), "or": ("and",)})
ARITHMETIC = Operator("arithmetic", {"+": ("-",), "-": ("+",), "*": ("/",), "/": ("*",)})
CONSTANT = Operator("constant", {"true": ("false",), "false": ("true",)})

#: The default catalog, in a stable order.
CATALOG: tuple[Operator, ...] = (COMPARISON, BOOLEAN, ARITHMETIC, CONSTANT)


def all_replacements(token: str, catalog: tuple[Operator, ...] = CATALOG) -> list[tuple[str, str]]:
    """Every ``(operator_id, replacement)`` candidate the catalog produces for `token`."""
    return [(op.id, repl) for op in catalog for repl in op.replacements(token)]
