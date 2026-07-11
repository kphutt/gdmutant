"""Unit tests for the language-neutral source-span editor."""

import pytest

from gdmutant.engine.spans import Span, apply_replacement


def test_replace_single_char_operator() -> None:
    # "if a > b:" — '>' is the 6th character (1-indexed), end column 7.
    assert apply_replacement("if a > b:\n", Span(1, 6, 1, 7), ">=") == "if a >= b:\n"


def test_replace_word_operator_and_to_or() -> None:
    # "return a and b" — "and" spans columns 10..13 (exclusive).
    assert apply_replacement("return a and b\n", Span(1, 10, 1, 13), "or") == "return a or b\n"


def test_replacement_preserves_other_lines() -> None:
    src = "line one\nif a > b:\nline three\n"
    assert apply_replacement(src, Span(2, 6, 2, 7), ">=") == "line one\nif a >= b:\nline three\n"


def test_no_trailing_newline_on_last_line() -> None:
    assert apply_replacement("a > b", Span(1, 3, 1, 4), ">=") == "a >= b"


def test_crlf_line_endings_preserved() -> None:
    result = apply_replacement("if a > b:\r\nx = 1\r\n", Span(1, 6, 1, 7), ">=")
    assert result == "if a >= b:\r\nx = 1\r\n"


def test_replacement_can_be_empty_or_longer() -> None:
    assert apply_replacement("a + b\n", Span(1, 3, 1, 4), "") == "a  b\n"
    assert apply_replacement("a + b\n", Span(1, 3, 1, 4), "plus") == "a plus b\n"


def test_multiline_span_rejected() -> None:
    with pytest.raises(ValueError, match="multi-line"):
        apply_replacement("a\nb\n", Span(1, 1, 2, 1), "x")


def test_line_out_of_range() -> None:
    with pytest.raises(IndexError):
        apply_replacement("a\n", Span(5, 1, 5, 2), "x")


def test_column_out_of_range() -> None:
    with pytest.raises(IndexError):
        apply_replacement("ab\n", Span(1, 1, 1, 9), "x")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line": 0, "column": 1, "end_line": 1, "end_column": 2},
        {"line": 1, "column": 0, "end_line": 1, "end_column": 2},
        {"line": 1, "column": 1, "end_line": 1, "end_column": 0},
    ],
)
def test_span_rejects_non_positive_coordinates(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        Span(**kwargs)


def test_span_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="precedes"):
        Span(1, 5, 1, 3)
