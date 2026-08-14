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


def test_numeric_bump_moves_a_float_by_one_unit_of_its_written_precision() -> None:
    # A whole 1.0 would be the wrong size of change: a 0.016 frame delta shifted to 1.016 is a
    # mutant any test that reaches the line kills, which raises the score without measuring
    # anything. One unit of the precision the author wrote is instead the off-by-a-bit bug a tuned
    # constant plausibly has, so a survivor there is a real, unpinned value.
    assert NUMERIC.replacements("3.5") == ("3.6", "3.4")
    assert NUMERIC.replacements("0.016") == ("0.017", "0.015")
    assert NUMERIC.replacements("-3.5") == ("-3.4", "-3.6")


def test_numeric_bump_carries_a_float_across_its_own_decimal_point() -> None:
    # The bump is arithmetic on the whole number, not a digit edit, so 0.9 bumped up is 1.0 rather
    # than a rolled-over 0.0 and 1.0 bumped down is 0.9 rather than a borrowed 0.10.
    assert NUMERIC.replacements("0.9") == ("1.0", "0.8")
    assert NUMERIC.replacements("1.0") == ("1.1", "0.9")


def test_numeric_bump_keeps_a_floats_bare_or_trailing_point() -> None:
    # GDScript accepts both `.5` and `1.`, and a mutant has to stay the form it started as, or it
    # reformats the line as well as mutating it. `.9` is where that gives way: the integer digit is
    # needed to write the value at all, so it appears.
    assert NUMERIC.replacements(".5") == (".6", ".4")
    assert NUMERIC.replacements("1.") == ("2.", "0.")
    assert NUMERIC.replacements(".9") == ("1.0", ".8")


def test_numeric_bump_leaves_an_exponent_suffix_alone() -> None:
    # Bumping the mantissa bumps the value; rewriting the exponent would be a far larger, different
    # mutation. The suffix comes back exactly as written, sign and case included.
    assert NUMERIC.replacements("1.5e-3") == ("1.6e-3", "1.4e-3")
    assert NUMERIC.replacements("1.5E+10") == ("1.6E+10", "1.4E+10")
    assert NUMERIC.replacements("1e3") == ("2e3", "0e3")


def test_numeric_bump_works_in_a_hex_or_binary_literals_own_base() -> None:
    # ±1 means ±1 of the value, written back in the base the literal uses -- not ±1 on its digits.
    assert NUMERIC.replacements("0x1f") == ("0x20", "0x1e")
    assert NUMERIC.replacements("0b1010") == ("0b1011", "0b1001")
    assert NUMERIC.replacements("-0xff") == ("-0xfe", "-0x100")


def test_numeric_bump_keeps_hex_digit_case_and_written_width() -> None:
    # `0x0F` is a deliberate two-nibble constant and its mutants read as ones too. Padding only ever
    # adds, so the bump that needs a third nibble still gets it.
    assert NUMERIC.replacements("0xFF") == ("0x100", "0xFE")
    assert NUMERIC.replacements("0x0F") == ("0x10", "0x0E")


def test_numeric_bump_keeps_decimal_zero_padding() -> None:
    # `007` is written three digits wide on purpose; bumping it to `8` would reformat the line.
    assert NUMERIC.replacements("007") == ("008", "006")


def test_numeric_bump_regroups_digit_separators() -> None:
    # A separated literal keeps its spacing, including through the bump that changes the digit
    # count -- `1_000_000` down is `999_999`, not `1000_000` or an unreadable `999999`.
    assert NUMERIC.replacements("1_000_000") == ("1_000_001", "999_999")
    assert NUMERIC.replacements("999_999") == ("1_000_000", "999_998")
    assert NUMERIC.replacements("0b1010_1010") == ("0b1010_1011", "0b1010_1001")
    assert NUMERIC.replacements("1_000.5") == ("1_000.6", "1_000.4")
    assert NUMERIC.replacements("0.000_001") == ("0.000_002", "0.000_000")


def test_an_irregularly_separated_literal_loses_its_spacing_not_its_mutants() -> None:
    # Separators that aren't evenly spaced describe no grouping to carry onto a different digit
    # count, so the mutant is written plain. That is not the same as dropping it: the operator used
    # to skip every separated literal outright, which is the coverage hole this all closes.
    assert NUMERIC.replacements("1_00_000") == ("100001", "99999")


def test_numeric_skips_anything_that_is_not_a_literal() -> None:
    # "-0" and "-0.0" are excluded as well: neither is a real negative literal, and their bumps
    # wouldn't round-trip. `0b1234` is shaped like a radix literal but does not fit base 2, and
    # `1__0` / `1_` put a separator where no language allows one, so none of them is a number.
    for non_literal in (
        "and",
        "",
        "-",
        ".",
        "e3",
        "-0",
        "-0.0",
        "-0x0",
        "0b1234",
        "1__0",
        "1_",
        "0X1f",
        "٣",  # noqa: RUF001 (Arabic-Indic 3: a digit, but not one any of these languages writes)
    ):
        assert NUMERIC.replacements(non_literal) == (), f"unexpectedly mutated {non_literal!r}"


def test_numeric_bump_is_reversible_across_every_literal_form() -> None:
    # n is reachable from each of its bumps, so every numeric mutation can be undone -- and for
    # each form below the *spelling* comes back too, not just the value.
    for token in (
        "0",
        "1",
        "7",
        "100",
        "-1",
        "-5",
        "007",
        "3.5",
        "-3.5",
        "0.016",
        ".5",
        "1.",
        "1.5e-3",
        "0x1f",
        "0b1010",
        "1_000_000",
        "999_999",
    ):
        for repl in NUMERIC.replacements(token):
            assert token in NUMERIC.replacements(repl), f"{token} -> {repl} is not reversible"


def test_a_bump_that_loses_its_hex_letters_returns_the_value_not_the_spelling() -> None:
    # The one place the round trip cannot restore the original text, pinned so it stays a known,
    # bounded limit rather than a surprise: `0x100` has no letters left to record that it was
    # written uppercase, so its own bump back down is `0x0ff`. Same number, different spelling.
    bumped_up = NUMERIC.replacements("0xFF")[0]
    assert bumped_up == "0x100"
    assert NUMERIC.replacements(bumped_up)[1] == "0x0ff"
    assert int("0x0ff", 16) == int("0xFF", 16)


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
