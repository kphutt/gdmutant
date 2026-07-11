"""Tests for the language-neutral operator catalog."""

import pytest

from gdmutant.engine.operators import (
    ARITHMETIC,
    BOOLEAN,
    CATALOG,
    COMPARISON,
    CONSTANT,
    NUMERIC,
    TableOperator,
    all_replacements,
    applies,
)

_TABLE_OPS = [op for op in CATALOG if isinstance(op, TableOperator)]


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


def test_constant_boolean_swaps() -> None:
    assert CONSTANT.replacements("true") == ("false",)
    assert CONSTANT.replacements("false") == ("true",)


def test_numeric_bump() -> None:
    assert NUMERIC.replacements("0") == ("1",)  # no -1: replacements stay non-negative
    assert NUMERIC.replacements("5") == ("6", "4")
    assert NUMERIC.replacements("42") == ("43", "41")


def test_numeric_only_applies_to_bare_decimal_integers() -> None:
    for non_int in ("and", "3.5", "0x1f", "1_000", "", "-3", "٣"):  # noqa: RUF001 (Arabic-Indic 3)
        assert NUMERIC.replacements(non_int) == (), f"unexpectedly mutated {non_int!r}"


def test_numeric_bump_is_reversible() -> None:
    # n is reachable from each of its bumps, so every numeric mutation can be undone.
    for token in ("0", "1", "7", "100"):
        for repl in NUMERIC.replacements(token):
            assert token in NUMERIC.replacements(repl)


def test_non_applicable_token_yields_nothing() -> None:
    assert COMPARISON.replacements("and") == ()
    assert applies(COMPARISON, "and") is False
    assert applies(COMPARISON, ">") is True
    assert applies(NUMERIC, "9") is True
    assert applies(NUMERIC, "x") is False


@pytest.mark.parametrize("op", _TABLE_OPS)
def test_no_table_operator_maps_a_token_to_itself(op: TableOperator) -> None:
    # A mutation must change something — a no-op "mutant" would always survive and mean nothing.
    for token, repls in op.table.items():
        assert token not in repls, f"{op.id} maps {token!r} to itself"


@pytest.mark.parametrize("op", _TABLE_OPS)
def test_table_swaps_are_involutive(op: TableOperator) -> None:
    for token, repls in op.table.items():
        for repl in repls:
            assert token in op.replacements(repl), f"{op.id}: {token}->{repl} is not reversible"


def test_catalog_has_unique_ids() -> None:
    ids = [op.id for op in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_contents() -> None:
    assert CATALOG == (COMPARISON, BOOLEAN, ARITHMETIC, CONSTANT, NUMERIC)


def test_all_replacements_collects_across_catalog() -> None:
    assert all_replacements(">") == [("comparison", ">=")]
    assert all_replacements("and") == [("boolean", "or")]
    assert all_replacements("0") == [("numeric", "1")]
    assert all_replacements("nonsense") == []


def test_all_replacements_respects_a_custom_catalog() -> None:
    only_bool = (BOOLEAN,)
    assert all_replacements(">", catalog=only_bool) == []
    assert all_replacements("or", catalog=only_bool) == [("boolean", "and")]
