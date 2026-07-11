"""Tests for the language-neutral operator catalog."""

import pytest

from gdmutant.engine.operators import (
    ARITHMETIC,
    BOOLEAN,
    CATALOG,
    COMPARISON,
    CONSTANT,
    Operator,
    all_replacements,
)


def test_comparison_swaps() -> None:
    assert COMPARISON.replacements(">") == (">=",)
    assert COMPARISON.replacements(">=") == (">",)
    assert COMPARISON.replacements("==") == ("!=",)
    assert COMPARISON.replacements("!=") == ("==",)


def test_boolean_swaps() -> None:
    assert BOOLEAN.replacements("and") == ("or",)
    assert BOOLEAN.replacements("or") == ("and",)


def test_arithmetic_swaps() -> None:
    assert ARITHMETIC.replacements("+") == ("-",)
    assert ARITHMETIC.replacements("/") == ("*",)


def test_constant_swaps() -> None:
    assert CONSTANT.replacements("true") == ("false",)
    assert CONSTANT.replacements("false") == ("true",)


def test_non_applicable_token_yields_nothing() -> None:
    assert COMPARISON.replacements("and") == ()
    assert COMPARISON.applies_to("and") is False
    assert COMPARISON.applies_to(">") is True


@pytest.mark.parametrize("op", CATALOG)
def test_no_operator_maps_a_token_to_itself(op: Operator) -> None:
    # A mutation must change something — a no-op "mutant" would always survive and mean nothing.
    for token, repls in op.table.items():
        assert token not in repls, f"{op.id} maps {token!r} to itself"


@pytest.mark.parametrize("op", CATALOG)
def test_swaps_are_involutive(op: Operator) -> None:
    # Applying a swap and then swapping the result should be able to return to the original,
    # so every mutation is reversible (a sanity property for symmetric operators).
    for token, repls in op.table.items():
        for repl in repls:
            assert token in op.replacements(repl), f"{op.id}: {token}->{repl} is not reversible"


def test_catalog_has_unique_ids() -> None:
    ids = [op.id for op in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_contents() -> None:
    assert CATALOG == (COMPARISON, BOOLEAN, ARITHMETIC, CONSTANT)


def test_all_replacements_collects_across_catalog() -> None:
    assert all_replacements(">") == [("comparison", ">=")]
    assert all_replacements("and") == [("boolean", "or")]
    assert all_replacements("nonsense") == []


def test_all_replacements_respects_a_custom_catalog() -> None:
    only_bool = (BOOLEAN,)
    assert all_replacements(">", catalog=only_bool) == []
    assert all_replacements("or", catalog=only_bool) == [("boolean", "and")]
