"""Tests for the GDScript adapter's mutation half."""

from gdmutant.adapters.gdscript import (
    apply_mutant,
    find_sites,
    generate_mutants,
    is_valid_gdscript,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import TableOperator


def test_find_sites_locates_operators_and_literals() -> None:
    sites = find_sites("func f(a, b):\n\treturn a > b and b >= 0\n")
    assert {s.token for s in sites} == {">", "and", ">=", "0"}


def test_find_sites_ignores_strings_and_comments() -> None:
    # gdtoolkit doesn't tokenize inside strings/comments, so the operators there are never sites.
    src = 'func f(a, b):\n\tvar s := "a > b and 5"\n\t# c > d or 7\n\treturn a > b\n'
    sites = find_sites(src)
    assert [(s.token, s.span.line) for s in sites] == [(">", 4)]  # only the real one, on line 4


def test_generate_mutants_records_path_and_every_mutant_is_valid() -> None:
    src = "func f(a, b):\n\treturn a > b and b >= 0\n"
    mutants = generate_mutants("f.gd", src)
    assert mutants and all(m.path == "f.gd" for m in mutants)
    for m in mutants:
        _mutated, valid = apply_mutant(m, src)
        assert valid, f"catalog mutant should be valid GDScript: {m}"


def test_find_sites_pins_duplicate_tokens_in_document_order() -> None:
    # Two '>' on one line must yield TWO distinct sites (a dedup-by-value bug would silently drop
    # the second, under-generating mutants — the product's core output). Sites come back in
    # document (left-to-right) order.
    sites = find_sites("func f(a, b, c, d):\n\treturn a > b and c > d\n")
    assert [(s.token, s.span.column) for s in sites] == [(">", 11), ("and", 15), (">", 21)]


def test_find_sites_and_generate_thread_a_custom_catalog_through() -> None:
    # A custom operator that mutates a token the DEFAULT catalog ignores (the identifier `b`).
    # Both find_sites and generate_mutants must use THIS catalog — a mutant that falls back to the
    # default would never surface `b`, so it finds nothing here.
    custom = (TableOperator("ident", {"b": ("c",)}),)
    src = "func f(a, b):\n\treturn b > a\n"
    assert {s.token for s in find_sites(src, catalog=custom)} == {"b"}
    mutants = generate_mutants("f.gd", src, catalog=custom)
    assert mutants and {(m.original, m.replacement) for m in mutants} == {("b", "c")}


def test_negative_literal_is_located_and_mutated() -> None:
    # gdtoolkit tokenizes -5 as a single NUMBER token; the numeric operator must still bump it.
    src = "func f():\n\treturn -5\n"
    assert any(s.token == "-5" for s in find_sites(src))
    mutants = generate_mutants("f.gd", src)
    assert any(m.operator_id == "numeric" and m.original == "-5" for m in mutants)


def test_is_valid_gdscript() -> None:
    assert is_valid_gdscript("func f():\n\treturn 1\n") is True
    assert is_valid_gdscript("func f(:\n") is False


def test_apply_mutant_flags_invalid_mutant_nf5() -> None:
    # Real catalog swaps stay valid, so hand-build a mutant whose replacement breaks syntax to
    # exercise the NF-5 gate directly (an invalid mutant must report is_valid=False, never killed).
    src = "func f(a, b):\n\treturn a > b\n"
    site = next(s for s in find_sites(src) if s.token == ">")
    broken = Mutant("f.gd", site.span, "comparison", ">", ")")
    mutated, valid = apply_mutant(broken, src)
    assert "a ) b" in mutated
    assert valid is False
