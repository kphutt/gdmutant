"""Integration test proving the ADR-0002 mechanism against the real gdtoolkit parser:
locate an operator token's span → replace it in source → the mutant re-parses (NF-5).
"""

from lark import Token, Tree

from gdmutant.engine.spans import Span, apply_replacement


def _parse(code: str) -> Tree:
    from gdtoolkit.parser import parser

    return parser.parse(code, gather_metadata=True)


def _first_token(tree: Tree, value: str) -> Token:
    for tok in tree.scan_values(lambda v: isinstance(v, Token) and v.value == value):
        return tok
    raise AssertionError(f"no {value!r} token found")


def test_locate_and_mutate_roundtrip_reparses() -> None:
    src = "func f(a, b):\n\tif a > b:\n\t\treturn true\n\treturn false\n"
    tok = _first_token(_parse(src), ">")
    span = Span(tok.line, tok.column, tok.end_line, tok.end_column)

    mutated = apply_replacement(src, span, ">=")

    assert "a >= b" in mutated
    # NF-5: the mutant must still be valid GDScript.
    _parse(mutated)  # must not raise


def test_boolean_operator_span_is_located_and_mutated() -> None:
    src = "func f(a, b):\n\treturn a and b\n"
    tok = _first_token(_parse(src), "and")
    span = Span(tok.line, tok.column, tok.end_line, tok.end_column)

    mutated = apply_replacement(src, span, "or")

    assert "return a or b" in mutated
    _parse(mutated)


def test_special_line_char_before_mutation_does_not_desync() -> None:
    # A form feed inside a string literal is NOT a line boundary to gdtoolkit/lark (it counts
    # only "\n"), though Python's str.splitlines() would treat it as one. The span editor must
    # agree with the parser's line numbering, or it edits the wrong line. (Regression for the
    # ADR-0002 P2.)
    src = 'func f(a, b):\n\tvar s = "x\x0cy"\n\tif a > b:\n\t\treturn true\n\treturn false\n'
    tok = _first_token(_parse(src), ">")
    span = Span(tok.line, tok.column, tok.end_line, tok.end_column)

    mutated = apply_replacement(src, span, ">=")

    assert "a >= b" in mutated
    _parse(mutated)  # NF-5: still valid GDScript
