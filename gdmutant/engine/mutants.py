"""The language-neutral mutant model and generation.

A `MutationSite` is a located source token an adapter found — the token's text and its `Span`.
Given the sites and the operator catalog, `generate` yields one `Mutant` per
(site, operator-replacement): a **single isolated change** (DESIGN.md FG-1.2). Applying a `Mutant`
produces the mutated source via `engine.spans`; the adapter then validates (NF-5) and runs it.

The engine stays language-neutral: it consumes located sites (the adapter finds them via its AST)
and never tokenizes source itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from gdmutant.engine.operators import CATALOG, Operator
from gdmutant.engine.spans import Span, apply_replacement


@dataclass(frozen=True)
class MutationSite:
    """A located source token: its text and where it is (supplied by the adapter's AST)."""

    token: str
    span: Span


@dataclass(frozen=True)
class Mutant:
    """One isolated mutation: replace `original` at `span` with `replacement`.

    `operator_id` records which catalog operator produced it; `path` identifies the source file.
    Both are informational for reporting — `apply` edits whatever source text it is handed.
    """

    path: str
    span: Span
    operator_id: str
    original: str
    replacement: str

    def apply(self, source: str) -> str:
        """Return `source` with this mutation applied (one span edit)."""
        return apply_replacement(source, self.span, self.replacement)


def generate(
    path: str,
    sites: Iterable[MutationSite],
    catalog: tuple[Operator, ...] = CATALOG,
) -> list[Mutant]:
    """One `Mutant` per (site, operator replacement) — each a single isolated change (FG-1.2).

    Deterministic: sites in the given order, catalog in its order, replacements in operator order
    (NF-1). A site whose token no operator mutates contributes no mutants.
    """
    return [
        Mutant(
            path=path,
            span=site.span,
            operator_id=op.id,
            original=site.token,
            replacement=repl,
        )
        for site in sites
        for op in catalog
        for repl in op.replacements(site.token)
    ]
