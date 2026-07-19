"""Tests for the survivor-explanation renderer ([ticket])."""

from __future__ import annotations

from gdmutant.engine.explain import (
    _block,
    _display_col,
    _enclosing_func,
    doc_url,
    render_survivor,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.spans import Span


def _mutant(op: str, original: str, replacement: str, line: int = 2, col: int = 6) -> Mutant:
    return Mutant("Foo.gd", Span(line, col, line, col + len(original)), op, original, replacement)


#: The whole rendered block, byte-for-byte — pins the assembly (header, path, caret line, labels,
#: blank lines, the doc link) AND the comparison copy, so any drift in structure or wording is
#: caught. Update deliberately when the locked format changes.
_GOLDEN_COMPARISON = """\
──── survived ──────────────────────────────────────────── comparison ────

  V.gd:2   func is_greater

      2 |     if _major > other._major:
        |               ^  changed  >  to  >= — every test still passed

  gap    Your tests pass whether this says `>` or `>=`. They run this
         line, but never the one input where the two disagree — equal
         operands. That case is untested.

  risk   Passing here is false confidence, not proof. A later refactor or
         merge that changes the equal case slips through green. If the
         equal case has a right answer, no test guards it.

  start  Add a test that reaches this line with two equal operands (a
         value compared to itself) and assert the result you expect. Only
         you know that result — gdmutant reports the gap, not it.

  more   https://github.com/kphutt/gdmutant/blob/main/docs/survivors/comparison.md
──────────────────────────────────────────────────────────────────────────"""


def test_render_matches_the_golden_block_exactly() -> None:
    src = ["func is_greater(other):", "\tif _major > other._major:"]
    m = Mutant("V.gd", Span(2, 12, 2, 13), "comparison", ">", ">=")
    assert "\n".join(render_survivor(m, src)) == _GOLDEN_COMPARISON


def test_full_block_shows_code_caret_and_enclosing_func() -> None:
    src = ["func is_greater(other):", "\tif _major > other._major:", "\t\treturn true"]
    # column 6 is the `>` inside the tab-indented line 2 (1 tab + "if _major ")
    out = "\n".join(render_survivor(_mutant("comparison", ">", ">=", line=2, col=11), src))
    assert "── survived " in out and " comparison ─" in out
    assert "Foo.gd:2   func is_greater" in out  # enclosing function from the AST scan
    assert "if _major > other._major:" in out  # the source line, as written
    assert "^  changed  >  to  >= — every test still passed" in out
    assert "gap    Your tests pass whether this says `>` or `>=`" in out
    assert "risk   Passing here is false confidence" in out
    assert "start  Add a test that reaches this line with two equal operands" in out
    assert "reports the gap, not it" in out  # declines to assert the oracle
    assert "docs/survivors/comparison.md" in out


def test_caret_lands_under_the_token_across_tabs() -> None:
    # A tab is width-4; the caret must land under the mutated token, not count a tab as one column.
    src = ["\tif a > b:"]  # `>` is at 1-based column 7 (tab + "if a ")
    lines = render_survivor(_mutant("comparison", ">", ">=", line=1, col=7), src)
    code = next(x for x in lines if x.lstrip().startswith("1 |"))
    caret = next(x for x in lines if "^" in x)
    # the caret index equals the '>' index in the expanded (tab->4 spaces) code line
    assert caret.index("^") == code.index(">")


def test_no_source_degrades_gracefully_but_keeps_the_narrative() -> None:
    lines = render_survivor(_mutant("boolean", "and", "or"), None)
    out = "\n".join(lines)
    assert "boolean ─" in out
    assert "gap    Your tests pass whether this needs both sides" in out
    assert "start" in out and "docs/survivors/boolean.md" in out
    # no code line, no caret, no enclosing-func when the source is unavailable
    assert "|" not in out and "^" not in out and "func " not in out
    # the path line is exactly the location — no trailing junk from the missing-func branch
    assert "  Foo.gd:2" in lines


def test_out_of_range_line_is_treated_as_no_source() -> None:
    out = "\n".join(render_survivor(_mutant("boolean", "and", "or", line=99), ["one line only"]))
    assert "^" not in out and "gap" in out


def test_logical_not_removal_reads_as_removed_not_a_deleted_line() -> None:
    src = ["\tif not ready:"]
    out = "\n".join(render_survivor(_mutant("logical-not", "not", "", line=1, col=5), src))
    assert "removed  not — every test still passed" in out
    assert "this whole line was removed" not in out
    assert "-> " not in out  # no dangling arrow ([ticket])


def test_statement_deletion_reads_as_whole_line_removed() -> None:
    src = ["\tprint(x)"]
    m = _mutant("statement-deletion", "print(x)", "", line=1, col=2)
    out = "\n".join(render_survivor(m, src))
    assert "this whole line was removed — every test still passed" in out
    assert "gap    Your tests pass with this line removed entirely" in out


def test_unknown_operator_uses_the_safe_fallback() -> None:
    out = "\n".join(render_survivor(_mutant("custom-op", "x", "y"), None))
    assert "gap    Your tests pass with this change applied" in out
    assert "start  Add a test that fails under this exact change" in out
    assert "docs/survivors/custom-op.md" in out


def test_enclosing_func_handles_static_func_and_none() -> None:
    lines = ["static func parse(v):", "\tvar x := 1", "func other():", "\tpass"]
    assert _enclosing_func(lines, 2) == "parse"  # inside the static func
    assert _enclosing_func(lines, 4) == "other"
    assert _enclosing_func(["var top := 1"], 1) is None  # no enclosing func
    # the scan starts at the mutant's OWN line (a func declaration counts as its own encloser)...
    assert _enclosing_func(["func target():", "\tx"], 1) == "target"
    # ...and stops at the top — a func BELOW the line is not an encloser (guards the range bounds).
    assert _enclosing_func(["var x", "func late():"], 1) is None
    # a param default that itself calls something has an inner "(" — split on the FIRST "(" only,
    # or the name is misread as "f(x = g" (guards split-vs-rsplit and the maxsplit).
    assert _enclosing_func(["func f(x = g()):", "\ty"], 1) == "f"


def test_block_handles_empty_text() -> None:
    # A slot with empty text still renders its label line and nothing else (the ``or [""]`` guard).
    assert _block("gap", "") == ["  " + "gap".ljust(7)]


def test_display_col_expands_tabs() -> None:
    assert _display_col("") == 0
    assert _display_col("ab") == 2
    assert _display_col("\t") == 4  # one tab -> width 4
    assert _display_col("\tab") == 6
    assert _display_col("ab\t") == 4  # a tab mid-line advances to the next multiple of 4 (not +)


def test_doc_url_is_the_stable_per_operator_page() -> None:
    assert doc_url("comparison").endswith("/docs/survivors/comparison.md")
    assert doc_url("comparison").startswith("https://")


def test_every_operator_has_a_docs_page_so_the_more_link_is_never_dead() -> None:
    # The `more` link points at docs/survivors/<operator>.md — a new operator must ship its page,
    # or the link 404s. Guards that drift.
    from pathlib import Path

    from gdmutant.engine.explain import _EXPLAIN

    survivors_dir = Path(__file__).resolve().parent.parent / "docs" / "survivors"
    for op in _EXPLAIN:
        assert (survivors_dir / f"{op}.md").is_file(), f"missing docs page for {op}"
