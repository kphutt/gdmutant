"""Tests for the language-neutral mutant model and generation."""

import pytest
from lark import Token, Tree

from gdmutant.engine.mutants import Mutant, MutationSite, generate
from gdmutant.engine.operators import BOOLEAN, all_replacements
from gdmutant.engine.spans import Span


def test_mutant_apply_edits_the_span() -> None:
    m = Mutant("x.gd", Span(1, 3, 1, 4), "comparison", ">", ">=")
    assert m.apply("a > b\n") == "a >= b\n"


def test_apply_rejects_span_original_mismatch() -> None:
    # A Mutant whose recorded `original` doesn't match the text at its span must fail fast, never
    # silently clobber a different token (the NF-5 worst case). Here the span points at ">" but
    # `original` claims "==".
    m = Mutant("x.gd", Span(1, 3, 1, 4), "comparison", "==", "!=")
    with pytest.raises(ValueError, match="mismatch"):
        m.apply("a > b\n")


def test_generate_one_mutant_per_replacement() -> None:
    (m,) = generate("x.gd", [MutationSite(">", Span(1, 3, 1, 4))])
    assert (m.path, m.operator_id, m.original, m.replacement) == ("x.gd", "comparison", ">", ">=")
    assert m.span == Span(1, 3, 1, 4)


def test_generate_multiple_replacements_from_one_site() -> None:
    # A numeric literal bumps to two candidates (n+1, n-1).
    mutants = generate("x.gd", [MutationSite("5", Span(1, 1, 1, 2))])
    assert [(m.operator_id, m.replacement) for m in mutants] == [("numeric", "6"), ("numeric", "4")]


def test_generate_skips_a_token_no_operator_mutates() -> None:
    assert generate("x.gd", [MutationSite("foo", Span(1, 1, 1, 4))]) == []


def test_generate_is_deterministic_and_ordered() -> None:
    sites = [MutationSite(">", Span(1, 1, 1, 2)), MutationSite("and", Span(2, 1, 2, 4))]
    mutants = generate("x.gd", sites)
    assert [(m.original, m.replacement) for m in mutants] == [(">", ">="), ("and", "or")]
    assert generate("x.gd", sites) == mutants  # same inputs -> identical output (NF-1)


def test_generate_empty_sites() -> None:
    assert generate("x.gd", []) == []


def test_generate_respects_a_custom_catalog() -> None:
    sites = [MutationSite(">", Span(1, 1, 1, 2)), MutationSite("or", Span(2, 1, 2, 3))]
    mutants = generate("x.gd", sites, catalog=(BOOLEAN,))
    assert [(m.operator_id, m.original, m.replacement) for m in mutants] == [
        ("boolean", "or", "and")
    ]


# --- integration: real gdtoolkit tokens -> sites -> mutants -> each re-parses ---


def _sites_from_source(src: str) -> list[MutationSite]:
    """Locate every mutable token in `src` via gdtoolkit (a stand-in for the Slice 3 adapter)."""
    from gdtoolkit.parser import parser

    tree: Tree = parser.parse(src, gather_metadata=True)
    sites: list[MutationSite] = []
    for tok in tree.scan_values(lambda v: isinstance(v, Token)):
        if all_replacements(tok.value):
            sites.append(
                MutationSite(tok.value, Span(tok.line, tok.column, tok.end_line, tok.end_column))
            )
    return sites


def test_generate_from_real_tokens_and_every_mutant_reparses() -> None:
    from gdtoolkit.parser import parser

    src = "func f(a, b):\n\treturn a > b and b >= 0\n"
    mutants = generate("f.gd", _sites_from_source(src))

    assert mutants, "expected at least one mutant"
    for m in mutants:
        parser.parse(m.apply(src), gather_metadata=True)  # NF-5: each mutant is valid GDScript

    kinds = {(m.operator_id, m.original, m.replacement) for m in mutants}
    assert ("comparison", ">", ">=") in kinds
    assert ("comparison", ">=", ">") in kinds
    assert ("boolean", "and", "or") in kinds
    assert ("numeric", "0", "1") in kinds
