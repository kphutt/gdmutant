"""Tests for marker placement (docs/decisions/0017, Plan step 1): one test per row of the ADR's
placement table, the cases between its rows, and a sweep over the corpus and the benchmark's
synthetic files. The real-Godot half (the marked source loads, keeps its line numbers, and its
markers fire) is in tests/test_selftest_live.py."""

from __future__ import annotations

import dataclasses
import functools
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest
from lark import Lark
from lark.exceptions import LarkError

from gdmutant.adapters.gdscript import STATEMENT_DELETION_ID, generate_mutants
from gdmutant.adapters.gdscript.markers import (
    MARKER_AUTOLOAD,
    MarkedSource,
    Placement,
    RunEverything,
    Spot,
    marker_call,
    place_markers,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.spans import Span

REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKER = re.compile(r"_GdmMarks\.hit\(\d+\); ")


@functools.cache
def _godot_grammar_parser() -> Lark:
    """gdtoolkit's own grammar plus the one rule a marked source needs that it lacks.

    gdtoolkit only allows a compound statement (``if``, ``while``, ``for``, ``match``) at the
    start of a line, so it rejects ``_GdmMarks.hit(1); if x:``. Godot accepts it (checked against
    Godot 4.7, and tests/test_selftest_live.py runs a marked corpus full of them). This adds
    exactly that rule, so everything else a marked source contains is still held to gdtoolkit's
    grammar. If gdtoolkit changes the rule this edits, the assert fails here instead of the check
    going quiet.
    """
    import gdtoolkit.parser as package
    from gdtoolkit.parser.gdscript_indenter import GDScriptIndenter

    folder = os.path.dirname(package.__file__)
    with open(os.path.join(folder, "gdscript.lark"), encoding="utf-8") as grammar_file:
        grammar = grammar_file.read()
    rule = "_func_stmt: _simple_func_stmt _NL\n"
    assert rule in grammar, "gdtoolkit's grammar changed, so update this helper"
    grammar = grammar.replace(rule, rule + "          | _simple_func_stmt compound_func_stmt\n")
    return Lark(
        grammar,
        parser="lalr",
        start="start",
        postlex=GDScriptIndenter(),  # type: ignore[no-untyped-call]
        maybe_placeholders=False,
        regex=True,
    )


def _assert_well_formed(original: str, result: MarkedSource) -> None:
    """The properties every marked source must have, whatever the input."""
    # Removing exactly the inserted calls gives back the original, byte for byte, so a marker
    # never changed anything else and never added or removed a line.
    assert _MARKER.sub("", result.source) == original
    assert result.source.count("\n") == original.count("\n")
    assert len(_MARKER.findall(result.source)) == len(result.spots)
    lines = result.source.split("\n")
    for spot in result.spots:
        # Each spot's marker is on its own line, at the column recorded for it in the original.
        assert marker_call(spot.id) in lines[spot.line - 1]
    _godot_grammar_parser().parse(result.source + "\n")


def _mark(source: str, first_spot: int = 0) -> tuple[list[Mutant], MarkedSource]:
    mutants = generate_mutants("t.gd", source)
    result = place_markers(source, mutants, first_spot)
    _assert_well_formed(source, result)
    return mutants, result


def _placements(source: str) -> dict[tuple[int, str], set[Placement]]:
    """(line, original text) -> the placements its mutants got, for compact asserts."""
    mutants, result = _mark(source)
    table: dict[tuple[int, str], set[Placement]] = {}
    for mutant, placement in zip(mutants, result.placements, strict=True):
        table.setdefault((mutant.span.line, mutant.original), set()).add(placement)
    return table


def test_marker_call_is_one_prefixable_statement() -> None:
    assert MARKER_AUTOLOAD == "_GdmMarks"
    assert marker_call(7) == "_GdmMarks.hit(7); "


def test_the_run_everything_reasons_read_as_short_phrases() -> None:
    # Step 2 counts and prints these, so each one is pinned as the text a user will see.
    assert {reason.name: reason.value for reason in RunEverything} == {
        "CLASS_LEVEL": "class-level code",
        "ANNOTATION": "annotation argument",
        "CONST": "const",
        "SINGLE_LINE_LAMBDA": "single-line lambda",
        "AWAIT": "await",
        "NO_BODY_STATEMENT": "no statement in the function body",
    }


def test_results_cannot_be_changed_after_the_fact() -> None:
    result = place_markers("func f(a):\n\treturn a > 1\n", [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.source = ""  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Spot(0, 1, 1).id = 2  # type: ignore[misc]


def test_spot_ids_start_at_zero_by_default() -> None:
    source = "func f(a):\n\treturn a > 1\n"
    result = place_markers(source, generate_mutants("t.gd", source))
    assert result.spots == (Spot(0, 2, 2),)
    assert set(result.placements) == {0}


@pytest.mark.parametrize("keyword", ["pass", "break", "continue", "breakpoint"])
def test_a_statement_after_a_keyword_statement_on_its_line_gets_its_own_marker(
    keyword: str,
) -> None:
    source = f"func f(xs):\n\tfor x in xs:\n\t\t{keyword}; x += 1\n"
    _, result = _mark(source)
    assert result.source.split("\n")[2] == f"\t\t{keyword}; _GdmMarks.hit(0); x += 1"


def test_a_keyword_statement_can_be_the_first_statement_a_default_value_is_marked_by() -> None:
    _, result = _mark("func f(a = 1):\n\tpass\n")
    assert result.source == "func f(a = 1):\n\t_GdmMarks.hit(0); pass\n"
    assert set(result.placements) == {0}


def test_a_typed_for_loop_is_a_statement_like_any_other() -> None:
    source = "func f(a, b):\n\tfor i: int in range(a + 1):\n\t\tpass\n\tfor j: int in b: a -= 2\n"
    _, result = _mark(source)
    assert result.source.split("\n")[1:4] == [
        "\t_GdmMarks.hit(0); for i: int in range(a + 1):",
        "\t\tpass",
        "\t_GdmMarks.hit(1); for j: int in b: a -= 2",
    ]


def test_a_header_line_body_inside_another_climbs_to_the_first_header_that_starts_a_line() -> None:
    source = "func f(a, b):\n\twhile a:\n\t\tif a: for i in b: a += 1\n"
    _, result = _mark(source)
    assert result.source.split("\n")[1:3] == [
        "\twhile a:",
        "\t\t_GdmMarks.hit(0); if a: for i in b: a += 1",
    ]


def test_a_const_inside_another_statement_still_runs_everything() -> None:
    source = "func f(a):\n\tif a:\n\t\tconst K = 1 + 2\n\t\tprint(K)\n"
    assert _placements(source)[(3, "+")] == {RunEverything.CONST}


def test_a_multi_line_lambda_ending_in_two_statements_on_one_line_is_still_multi_line() -> None:
    source = "func f():\n\treturn func(q):\n\t\tprint(q)\n\t\tprint(q); return q * 3\n"
    assert _placements(source)[(4, "*")] == {2}


def test_a_statement_after_a_semicolon_keeps_its_own_annotation_but_not_an_earlier_one() -> None:
    source = (
        "func f(a: int):\n"
        '\t@warning_ignore("x") var p = 1; @warning_ignore("integer_division") var q = a / 2\n'
    )
    _, result = _mark(source)
    assert result.source.split("\n")[1] == (
        '\t_GdmMarks.hit(0); @warning_ignore("x") var p = 1; _GdmMarks.hit(1); '
        '@warning_ignore("integer_division") var q = a / 2'
    )


def test_statements_in_a_function_body_each_get_their_own_marker_on_their_first_line() -> None:
    source = (
        "func f(a, b):\n"
        "\tvar x = a + 1\n"
        "\tx = x * 2\n"
        "\tprint(x > 0)\n"
        "\tif a > b:\n"
        "\t\tpass\n"
        "\twhile a < 3:\n"
        "\t\tpass\n"
        "\tfor i in range(a + 1):\n"
        "\t\tpass\n"
        "\tmatch a - 1:\n"
        "\t\t_:\n"
        "\t\t\tpass\n"
        "\treturn a - b\n"
    )
    _, result = _mark(source)
    assert result.source == (
        "func f(a, b):\n"
        "\t_GdmMarks.hit(0); var x = a + 1\n"
        "\t_GdmMarks.hit(1); x = x * 2\n"
        "\t_GdmMarks.hit(2); print(x > 0)\n"
        "\t_GdmMarks.hit(3); if a > b:\n"
        "\t\tpass\n"
        "\t_GdmMarks.hit(4); while a < 3:\n"
        "\t\tpass\n"
        "\t_GdmMarks.hit(5); for i in range(a + 1):\n"
        "\t\tpass\n"
        "\t_GdmMarks.hit(6); match a - 1:\n"
        "\t\t_:\n"
        "\t\t\tpass\n"
        "\t_GdmMarks.hit(7); return a - b\n"
    )
    assert result.spots == tuple(Spot(n, n + 2, 2) for n in range(4)) + (
        Spot(4, 7, 2),
        Spot(5, 9, 2),
        Spot(6, 11, 2),
        Spot(7, 14, 2),
    )


def test_an_elif_condition_is_marked_by_the_if_that_opens_its_chain() -> None:
    # `n > 50 -> n >= 50` is killed by grade(50), which evaluates the elif condition and never
    # enters its body. A marker in the body would miss exactly that test, so the condition takes
    # the marker of the `if` at the top of the chain, which runs on every call that reaches it.
    source = (
        "static func grade(n: int) -> String:\n"
        "\tif n > 90:\n"
        "\t\treturn 'A'\n"
        "\telif n > 50:\n"
        "\t\treturn str(n - 1)\n"
        "\telse:\n"
        "\t\treturn str(n + 1)\n"
    )
    table = _placements(source)
    assert table[(2, ">")] == {0}
    assert table[(4, ">")] == {0}
    assert table[(4, "50")] == {0}
    # A statement inside the elif body or the else body gets a marker of its own.
    assert table[(5, "-")] == {1}
    assert table[(7, "+")] == {2}


def test_a_match_pattern_and_its_guard_are_marked_by_the_match_statement() -> None:
    source = (
        "func f(a, b):\n\tmatch a:\n\t\t1:\n\t\t\tprint(b + 1)\n\t\t2 when b > 3:\n\t\t\tpass\n"
    )
    table = _placements(source)
    assert table[(3, "1")] == {0}
    assert table[(5, "2")] == {0}
    assert table[(5, ">")] == {0}
    assert table[(4, "+")] == {1}


def test_a_default_parameter_value_is_marked_by_the_first_statement_of_the_body() -> None:
    source = "func f(a = 1, b := 2 + 3):\n\tvar c = a\n\treturn b > c\n"
    _, result = _mark(source)
    table = _placements(source)
    assert table[(1, "1")] == {0}
    assert table[(1, "+")] == {0}
    assert table[(3, ">")] == {1}
    # The first statement takes a marker for the defaults even though no mutant sits in it.
    assert result.source.split("\n")[1] == "\t_GdmMarks.hit(0); var c = a"


def test_a_default_parameter_value_with_no_statement_to_mark_runs_everything() -> None:
    # gdtoolkit accepts a body of annotations alone. There is nothing to put a marker in front of.
    source = 'func f(a = 1):\n\t@warning_ignore("unused_parameter")\n'
    assert _placements(source) == {(1, "1"): {RunEverything.NO_BODY_STATEMENT}}


def test_a_body_on_its_headers_own_line_is_marked_by_the_header_statement() -> None:
    source = (
        "func f(a, b):\n"
        "\tif a > b: return a - 1\n"
        "\telif a < 0: return b - 2\n"
        "\telse: return b * 3\n"
        "\twhile a < 3: a += 1\n"
        "\tfor i in range(b): a -= 4\n"
        "\tmatch a:\n"
        "\t\t1: return 5\n"
    )
    _, result = _mark(source)
    table = _placements(source)
    for line in (2, 3, 4):
        assert all(placements == {0} for (at, _), placements in table.items() if at == line)
    assert table[(5, "+=")] == {1}
    assert table[(6, "-=")] == {2}
    assert table[(8, "5")] == {3}
    assert result.source.split("\n")[1] == "\t_GdmMarks.hit(0); if a > b: return a - 1"


def test_a_statement_after_a_semicolon_is_marked_right_in_front_of_it() -> None:
    source = "func f(a):\n\tvar b = a; b += 1\n\tif a: b -= 2; return b > 3\n"
    _, result = _mark(source)
    assert result.source == (
        "func f(a):\n"
        "\tvar b = a; _GdmMarks.hit(0); b += 1\n"
        "\t_GdmMarks.hit(1); if a: b -= 2; _GdmMarks.hit(2); return b > 3\n"
    )
    table = _placements(source)
    assert table[(2, "+=")] == {0}
    assert table[(3, "-=")] == {1}  # the header-line body's first statement: the `if` marker
    assert table[(3, ">")] == {2}


def test_two_identical_statements_on_one_line_get_a_marker_each() -> None:
    # lark compares trees by value, so a lookup by equality finds the first `a += 1` for both.
    source = "func f(a):\n\ta += 1; a += 1\n"
    mutants, result = _mark(source)
    assert result.source == "func f(a):\n\t_GdmMarks.hit(0); a += 1; _GdmMarks.hit(1); a += 1\n"
    assert [p for m, p in zip(mutants, result.placements, strict=True) if m.original == "+="] == [
        0,
        1,
    ]


def test_a_one_line_function_is_marked_right_after_its_colon() -> None:
    source = "func f(a = 1): return a > 0\nfunc g(): pass; return 2 + 3\n"
    _, result = _mark(source)
    assert result.source == (
        "func f(a = 1): _GdmMarks.hit(0); return a > 0\n"
        "func g(): pass; _GdmMarks.hit(1); return 2 + 3\n"
    )
    table = _placements(source)
    assert table[(1, "1")] == {0}
    assert table[(1, ">")] == {0}
    assert table[(2, "+")] == {1}


def test_a_property_accessor_body_is_marked_like_a_function_body() -> None:
    source = (
        "var _x := 3\nvar prop: int = 7:\n\tget: return _x + 1\n\tset(value):\n\t\t_x = value - 1\n"
    )
    _, result = _mark(source)
    assert result.source.split("\n")[2:5] == [
        "\tget: _GdmMarks.hit(0); return _x + 1",
        "\tset(value):",
        "\t\t_GdmMarks.hit(1); _x = value - 1",
    ]
    table = _placements(source)
    assert table[(1, "3")] == {RunEverything.CLASS_LEVEL}


def test_an_annotation_on_the_statements_line_stays_attached_to_it() -> None:
    source = (
        "func f(a: int):\n"
        '\t@warning_ignore("integer_division") var y = a / 2\n'
        '\t@warning_ignore("integer_division")\n'
        "\tvar z = a / 3\n"
    )
    _, result = _mark(source)
    assert result.source == (
        "func f(a: int):\n"
        '\t_GdmMarks.hit(0); @warning_ignore("integer_division") var y = a / 2\n'
        '\t@warning_ignore("integer_division")\n'
        "\t_GdmMarks.hit(1); var z = a / 3\n"
    )
    assert result.spots == (Spot(0, 2, 2), Spot(1, 4, 2))


def test_a_multi_line_lambda_marks_its_own_statements() -> None:
    source = (
        "func f(xs):\n"
        "\tvar g = func(q = 1 + 2):\n"
        "\t\tprint(q)\n"
        "\t\treturn q * 3\n"
        "\treturn xs.map(func(q):\n"
        "\t\treturn q - 4\n"
        "\t)\n"
    )
    _, result = _mark(source)
    table = _placements(source)
    assert table[(2, "+")] == {0}  # a lambda's default: its first statement's marker
    assert table[(4, "*")] == {1}
    assert table[(6, "-")] == {2}
    assert result.source.split("\n")[2] == "\t\t_GdmMarks.hit(0); print(q)"
    # The statements that create the lambdas hold no mutant, so they get no marker.
    assert result.source.split("\n")[1] == source.split("\n")[1]


def test_a_single_line_lambda_runs_everything() -> None:
    source = "func f(xs):\n\treturn xs.map(func(q = 5): return q * 2)\n"
    table = _placements(source)
    assert table[(2, "*")] == {RunEverything.SINGLE_LINE_LAMBDA}
    assert table[(2, "5")] == {RunEverything.SINGLE_LINE_LAMBDA}
    assert table[(2, "return q * 2")] == {RunEverything.SINGLE_LINE_LAMBDA}


def test_a_statement_containing_await_runs_everything() -> None:
    source = (
        "func f(a):\n"
        "\tvar t = await get_tree().create_timer(a + 0.5).timeout\n"
        "\tawait g(a - 1)\n"
        "\tif await g(a) > 1:\n"
        "\t\tpass\n"
        "\tif a > 2: await g(a)\n"
        "\treturn a * 3\n"
    )
    table = _placements(source)
    assert table[(2, "+")] == {RunEverything.AWAIT}
    assert table[(3, "-")] == {RunEverything.AWAIT}
    assert table[(3, "await g(a - 1)")] == {RunEverything.AWAIT}
    assert table[(4, ">")] == {RunEverything.AWAIT}
    assert table[(6, ">")] == {RunEverything.AWAIT}  # a header-line body runs after the marker
    assert table[(7, "*")] == {0}  # after the pauses, a statement of its own is marked as usual


def test_an_await_that_cannot_come_between_marker_and_mutant_does_not_count() -> None:
    source = (
        "func f(a):\n"
        "\tif a > 1:\n"
        "\t\tawait g(a)\n"
        "\tvar h = func():\n"
        "\t\tawait g(2)\n"
        "\tvar k = [a + 3, func(): await g(4)]\n"
        "\tfor i in range(a - 5):\n"
        "\t\tawait g(i)\n"
    )
    table = _placements(source)
    # The `if` condition runs before its body's await. An await inside a lambda pauses the
    # lambda, not the statement that creates it. A `for` iterable is evaluated once, up front.
    assert table[(2, ">")] == {0}
    assert table[(6, "+")] == {1}
    assert table[(7, "-")] == {2}


def test_a_while_condition_runs_everything_when_the_loop_body_awaits() -> None:
    # The condition runs again after the body, so after the pause, possibly in a later test.
    source = "func f(a):\n\twhile a < 3:\n\t\tawait g(a)\n\t\ta += 1\n\twhile a > 9:\n\t\ta -= 2\n"
    table = _placements(source)
    assert table[(2, "<")] == {RunEverything.AWAIT}
    assert table[(4, "+=")] == {0}  # a body statement of its own is marked after the pause
    assert table[(5, ">")] == {1}


@pytest.mark.parametrize(
    ("source", "original", "reason"),
    [
        ("var a = 1 + 2\n", "+", RunEverything.CLASS_LEVEL),
        ("static var a := 3\n", "3", RunEverything.CLASS_LEVEL),
        ("extends Node\n@onready var a = 4\n", "4", RunEverything.CLASS_LEVEL),
        ("@export var a := 5\n", "5", RunEverything.CLASS_LEVEL),
        ("var a: int = 6:\n\tget:\n\t\treturn a\n", "6", RunEverything.CLASS_LEVEL),
        ("enum E { A = 7 }\n", "7", RunEverything.CLASS_LEVEL),
        ("class Inner:\n\tvar a = 8\n", "8", RunEverything.CLASS_LEVEL),
        ("const A = 9\n", "9", RunEverything.CONST),
        ("func f():\n\tconst B = 10\n\treturn B\n", "10", RunEverything.CONST),
        ("@export_range(0, 11) var a := 1\n", "11", RunEverything.ANNOTATION),
        ('func f(a):\n\t@warning_ignore("x", 12)\n\tprint(a)\n', "12", RunEverything.ANNOTATION),
    ],
)
def test_code_that_cannot_take_a_marker_runs_everything(
    source: str, original: str, reason: RunEverything
) -> None:
    mutants, result = _mark(source)
    placements = {
        placement
        for mutant, placement in zip(mutants, result.placements, strict=True)
        if mutant.original == original
    }
    assert placements == {reason}
    assert reason.value  # every reason reads as a short phrase


def test_a_signal_declaration_runs_everything() -> None:
    # The catalog mutates nothing a signal declaration can hold, so no generated mutant lands on
    # one. The rule still has to hold if one ever does, so this builds one by hand on its name.
    source = "signal hit(amount: int)\n"
    mutant = Mutant("t.gd", Span(1, 8, 1, 11), "made-up", "hit", "miss")
    result = place_markers(source, [mutant])
    assert result.placements == (RunEverything.CLASS_LEVEL,)
    assert result.spots == ()
    assert result.source == source


def test_a_statement_deletion_is_marked_by_the_statement_it_deletes() -> None:
    source = "func f(a):\n\tif a:\n\t\treturn\n\tprint(a)\n"
    mutants, result = _mark(source)
    deletions = [
        placement
        for mutant, placement in zip(mutants, result.placements, strict=True)
        if mutant.operator_id == STATEMENT_DELETION_ID
    ]
    # A bare `return` holds no token at all: the statement itself is what starts at its span.
    assert deletions == [0, 1]
    assert result.spots == (Spot(0, 3, 3), Spot(1, 4, 2))


def test_spot_ids_count_up_from_first_spot_in_source_order_and_are_shared() -> None:
    source = "func f(a):\n\treturn a > 1 and a < 2\nfunc g(b):\n\treturn b - 3\n"
    mutants, result = _mark(source, first_spot=40)
    assert result.spots == (Spot(40, 2, 2), Spot(41, 4, 2))
    assert set(result.placements) == {40, 41}
    assert result.placements.count(40) == len([m for m in mutants if m.span.line == 2])
    assert "_GdmMarks.hit(40); return a > 1" in result.source


def test_no_mutants_means_no_markers_and_the_source_unchanged() -> None:
    source = "func f(a):\n\treturn a > 1\n"
    assert place_markers(source, []) == MarkedSource(source, (), ())


def test_a_mutant_from_another_source_is_refused() -> None:
    source = "func f(a):\n\treturn a > 1\n"
    (mutant, *_) = generate_mutants("t.gd", source)
    with pytest.raises(ValueError, match=r"^mutant does not match the source at Span\("):
        place_markers("func f(a):\n\treturn a + 1\n", [mutant])


def test_a_mutant_that_starts_inside_a_token_is_refused() -> None:
    # Its text matches the source, but no token or statement starts where it does, so there is
    # no telling which statement it belongs to. Refusing beats guessing.
    source = "func f(abc):\n\treturn abc\n"
    mutant = Mutant("t.gd", Span(2, 10, 2, 11), "made-up", "b", "x")
    with pytest.raises(ValueError, match=r"^no token or statement starts at Span\("):
        place_markers(source, [mutant])


def test_the_godot_grammar_helper_is_stricter_than_nothing() -> None:
    # The parse in `_assert_well_formed` is only a check if it can fail.
    with pytest.raises(LarkError):
        _godot_grammar_parser().parse("func f():\n\t_GdmMarks.hit(0); elif x:\n\t\tpass\n")


def _benchmark_synthetic_source(functions: int) -> str:
    path = REPO_ROOT / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("benchmark_for_markers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    text: str = module.synthetic_source(functions)
    return text


def _corpus_and_synthetic_sources() -> list[tuple[str, str]]:
    corpus = sorted((REPO_ROOT / "corpus").rglob("*.gd"))
    # Only the corpus's own scripts: the test framework addons are downloaded, not part of it.
    own = [p for p in corpus if "addons" not in p.relative_to(REPO_ROOT / "corpus").parts]
    sources = [(p.name, p.read_text(encoding="utf-8")) for p in own]
    return sources + [("synthetic-3.gd", _benchmark_synthetic_source(3))]


_SWEEP = _corpus_and_synthetic_sources()


@pytest.mark.parametrize(("name", "source"), _SWEEP, ids=[name for name, _ in _SWEEP])
def test_every_catalog_mutant_gets_exactly_one_placement(name: str, source: str) -> None:
    mutants, result = _mark(source)
    assert mutants, f"{name} has no mutants, so this test would check nothing"
    assert len(result.placements) == len(mutants)
    spot_ids = {spot.id for spot in result.spots}
    for placement in result.placements:
        assert isinstance(placement, RunEverything) or placement in spot_ids
    # Every spot is some mutant's: a marker nothing needs is a marker nobody asked for.
    assert spot_ids == {p for p in result.placements if not isinstance(p, RunEverything)}


def test_the_corpus_target_is_marked_exactly() -> None:
    source = (REPO_ROOT / "corpus" / "turn_order.gd").read_text(encoding="utf-8")
    _, result = _mark(source)
    assert [(spot.line, spot.column) for spot in result.spots] == [
        (8, 2),
        (13, 2),
        (14, 3),
        (15, 2),
        (16, 3),
        (22, 2),
        (27, 2),
        (32, 2),
    ]
    assert not any(isinstance(p, RunEverything) for p in result.placements)
