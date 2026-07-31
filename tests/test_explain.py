"""Tests for the survivor-explanation renderer."""

from __future__ import annotations

from gdmutant.engine.explain import (
    ASSERT_SECTION,
    _block,
    _display_col,
    _enclosing_func,
    doc_url,
    on_assert,
    reference_section,
    render_survivor,
    source_line,
    survivor_report_fields,
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

  more   https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md#comparison
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
    assert "docs/survivors/README.md#comparison" in out


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
    assert "start" in out and "docs/survivors/README.md#boolean" in out
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
    assert "-> " not in out  # no dangling arrow


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
    assert "docs/survivors/README.md#custom-op" in out


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


def test_survivor_report_fields_reuse_the_same_narrative_as_the_console_block() -> None:
    # The HTML-report fields are the exact gap/risk/start copy the console block renders (single
    # source: `_narrative`) — description = the gap, statusReason = risk + start, blank-line joined.
    m = _mutant("comparison", ">", ">=")
    description, status_reason = survivor_report_fields(m)
    gap, risk, start = (
        "Your tests pass whether this says `>` or `>=`. They run this line, but never the one "
        "input where the two disagree — equal operands. That case is untested.",
        "Passing here is false confidence, not proof. A later refactor or merge that changes the "
        "equal case slips through green. If the equal case has a right answer, no test guards it.",
        "Add a test that reaches this line with two equal operands (a value compared to itself) "
        "and assert the result you expect. Only you know that result — gdmutant reports the gap, "
        "not it.",
    )
    assert description == gap
    assert status_reason == f"{risk}\n\n{start}"


def test_survivor_report_fields_use_the_fallback_for_an_unknown_operator() -> None:
    # An operator with no bespoke copy falls back to the safe generic narrative (same _FALLBACK the
    # console block uses), so an unrecognized operator still gets non-empty report fields.
    description, status_reason = survivor_report_fields(_mutant("custom-op", "x", "y"))
    assert description == "Your tests pass with this change applied — nothing distinguishes it."
    assert status_reason == (
        "A change here would pass every test.\n\nAdd a test that fails under this exact change."
    )


def test_doc_url_is_the_stable_per_operator_anchor() -> None:
    # The `more` link is a section anchor into the merged survivor reference: the operator id is the
    # heading slug verbatim, so the URL ends in README.md#<operator>.
    assert doc_url("comparison").endswith("/docs/survivors/README.md#comparison")
    assert doc_url("comparison").startswith("https://")


def _readme_slugs() -> set[str]:
    """The GitHub heading-anchor slugs of every ``## …`` section in the merged survivor reference —
    lowercase, spaces → hyphens (the subset of GitHub's slug rules these headings exercise)."""
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "docs" / "survivors" / "README.md"
    slugs = set()
    for line in readme.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            slugs.add(line[3:].strip().lower().replace(" ", "-"))
    return slugs


def test_every_operator_more_link_resolves_to_a_real_anchor_in_the_merged_page() -> None:
    # The `more` link deep-links into docs/survivors/README.md#<operator>. Every operator's anchor
    # must exist as a heading in that merged page, or the link 404s. Guards that drift — a new
    # operator must add its section (with a heading that slugifies to the operator id).
    from gdmutant.engine.explain import _EXPLAIN

    slugs = _readme_slugs()
    for op in _EXPLAIN:
        anchor = doc_url(op).rsplit("#", 1)[1]
        assert anchor == op  # the operator id is the anchor verbatim
        assert anchor in slugs, f"no `## …` section slugifies to #{anchor} in the merged page"


# --- survivors inside an `assert` ---------------------------------------------------------------
#
# On defensive code these can be most of a file's survivors, and every one of them is unkillable by
# construction: a failed `assert` aborts the Godot process, so an in-process test cannot pass on
# the original and fail on the mutant. Handing that reader the `comparison` advice ("add a test with
# two equal operands") is advice nobody can follow, and it is what makes a whole report read as
# noise. The rule is one function, `on_assert`, and every surface routes through it.

_ASSERT_LINE = "\tassert(value > 0)"


def test_narrative_switches_to_the_assert_explanation_inside_an_assert() -> None:
    # `>` at column 15 is inside `assert(`, which closes at index 8.
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    gap, risk_start = survivor_report_fields(mutant, _ASSERT_LINE)
    assert "sits inside an `assert`" in gap
    assert "no in-process test can kill this one" in gap
    # The operator copy — the advice that cannot be followed here — must be gone, not merely joined.
    assert "equal operands" not in gap + risk_start
    assert "# gdmutant: ignore" in risk_start  # the escape hatch that does apply


def test_narrative_keeps_the_operator_explanation_off_an_assert() -> None:
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    gap, _ = survivor_report_fields(mutant, "\tif value > 0:")
    assert "equal operands" in gap and "assert" not in gap


def test_deleting_an_assert_statement_is_an_assert_survivor() -> None:
    # Statement deletion replaces the whole `assert(...)` with `pass`. Its token starts *before*
    # the paren, so the column test alone would miss it — the mutant's own text carries the answer.
    deletion = _mutant("statement-deletion", "assert(value > 0)", "pass", line=1, col=2)
    assert on_assert(deletion.original, deletion.span.column, _ASSERT_LINE)


def test_a_token_before_the_assert_paren_is_not_an_assert_survivor() -> None:
    # Column 5 is inside `push_`, ahead of the `assert(` that opens later on the line — so the
    # rule must not claim it. This is what keeps a trailing `# assert(...)` comment harmless too.
    line = "\tif x > 0: assert(y > 0)"
    assert not on_assert(">", 5, line)
    assert on_assert(">", 21, line)


def test_an_assert_lookalike_identifier_is_not_an_assert() -> None:
    # `my_assert(` and `helper.assert(` are ordinary calls; a failed one raises nothing special, so
    # a mutant inside them is an ordinary, killable survivor.
    assert not on_assert(">", 16, "\tmy_assert(a > b)")
    assert not on_assert(">", 20, "\thelper.assert(a > b)")


def test_an_unreadable_source_falls_back_to_the_operator_explanation() -> None:
    # With no source line there is nothing to read, so the narrative stays the operator's — still
    # accurate, just less specific. It must never guess "assert" from the operator alone.
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    assert not on_assert(mutant.original, mutant.span.column, None)
    gap, _ = survivor_report_fields(mutant, None)
    assert "equal operands" in gap


def test_source_line_returns_none_off_the_end_of_the_file() -> None:
    # A survivor whose file has since shrunk keeps its narrative; only the line-derived detail goes.
    mutant = _mutant("comparison", ">", ">=", line=9, col=15)
    assert source_line(mutant, ["only one line"]) is None
    assert source_line(mutant, None) is None


def test_reference_section_routes_an_assert_survivor_to_its_own_page_section() -> None:
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    assert reference_section(mutant, _ASSERT_LINE) == ASSERT_SECTION
    assert reference_section(mutant, "\tif value > 0:") == "comparison"


def test_rendered_assert_survivor_links_to_the_assert_section_not_the_operator() -> None:
    # The `more` link is the one thing a reader clicks. Sending them to #comparison would land them
    # on "add a test that reaches this line with two equal operands" — the exact wrong instruction.
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    block = render_survivor(mutant, [_ASSERT_LINE])
    assert block[-2] == f"  more   {doc_url(ASSERT_SECTION)}"
    # The header still names the operator: that IS what changed.
    assert "comparison" in block[0]
