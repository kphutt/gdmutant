"""Tests for the language-neutral operator catalog."""

import pytest

from gdmutant.engine.operators import (
    ARITHMETIC,
    BOOLEAN,
    CATALOG,
    COMPARISON,
    COMPOUND_ASSIGN,
    CONSTANT,
    LOGICAL_NOT,
    MODULO,
    NUMERIC,
    TableOperator,
    all_replacements,
    applies,
)

_TABLE_OPS = [op for op in CATALOG if isinstance(op, TableOperator)]
#: Operators whose swaps are a reversible pairing. `modulo` (one-directional) and `logical-not`
#: (a deletion to "") are non-involutive by design and are exercised by their own tests below.
_INVOLUTIVE_OPS = [COMPARISON, BOOLEAN, ARITHMETIC, CONSTANT, COMPOUND_ASSIGN]


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


def test_compound_assignment_swaps() -> None:
    assert COMPOUND_ASSIGN.replacements("+=") == ("-=",)
    assert COMPOUND_ASSIGN.replacements("-=") == ("+=",)
    assert COMPOUND_ASSIGN.replacements("*=") == ("/=",)
    assert COMPOUND_ASSIGN.replacements("/=") == ("*=",)
    assert COMPOUND_ASSIGN.replacements("=") == ()  # plain assignment is not mutated


def test_modulo_swaps() -> None:
    # Directional (not a reversible pairing): `%` -> `*`/`/`, but `*`/`/` are left to ARITHMETIC.
    assert MODULO.replacements("%") == ("*", "/")
    assert MODULO.replacements("*") == ()
    assert MODULO.replacements("/") == ()


def test_logical_not_deletes() -> None:
    # Removing `not` is modelled as a swap to the empty string; the adapter's NF-5 re-parse guards
    # any result that wouldn't parse.
    assert LOGICAL_NOT.replacements("not") == ("",)
    assert applies(LOGICAL_NOT, "not") is True
    assert LOGICAL_NOT.replacements("and") == ()  # only the `not` keyword


def test_numeric_bump() -> None:
    assert NUMERIC.replacements("0") == ("1", "-1")
    assert NUMERIC.replacements("5") == ("6", "4")
    assert NUMERIC.replacements("42") == ("43", "41")


def test_numeric_bump_handles_negative_literals() -> None:
    # gdtoolkit folds the sign into the NUMBER token, so "-5" is a single token.
    assert NUMERIC.replacements("-5") == ("-4", "-6")
    assert NUMERIC.replacements("-1") == ("0", "-2")


def test_numeric_only_applies_to_bare_decimal_integers() -> None:
    # "-0" is excluded too: it isn't a real negative literal and wouldn't round-trip.
    for non_int in ("and", "3.5", "-3.5", "0x1f", "1_000", "", "-", "-0", "٣"):  # noqa: RUF001 (AN 3)
        assert NUMERIC.replacements(non_int) == (), f"unexpectedly mutated {non_int!r}"


def test_numeric_bump_is_reversible() -> None:
    # n is reachable from each of its bumps, so every numeric mutation can be undone.
    for token in ("0", "1", "7", "100", "-1", "-5"):
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


@pytest.mark.parametrize("op", _INVOLUTIVE_OPS)
def test_swap_operators_are_involutive(op: TableOperator) -> None:
    # The reversible-pairing operators only (see _INVOLUTIVE_OPS): every swap can be undone, so a
    # mutation is always a clean flip. `modulo` and `logical-not` are intentionally not in this set.
    for token, repls in op.table.items():
        for repl in repls:
            assert token in op.replacements(repl), f"{op.id}: {token}->{repl} is not reversible"


def test_catalog_has_unique_ids() -> None:
    ids = [op.id for op in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_contents() -> None:
    assert CATALOG == (
        COMPARISON,
        BOOLEAN,
        ARITHMETIC,
        CONSTANT,
        NUMERIC,
        COMPOUND_ASSIGN,
        MODULO,
        LOGICAL_NOT,
    )


def test_all_replacements_collects_across_catalog() -> None:
    assert all_replacements(">") == [("comparison", ">=")]
    assert all_replacements("and") == [("boolean", "or")]
    assert all_replacements("0") == [("numeric", "1"), ("numeric", "-1")]
    assert all_replacements("nonsense") == []


def test_all_replacements_respects_a_custom_catalog() -> None:
    only_bool = (BOOLEAN,)
    assert all_replacements(">", catalog=only_bool) == []
    assert all_replacements("or", catalog=only_bool) == [("boolean", "and")]
