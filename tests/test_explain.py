"""Tests for the survivor-explanation renderer."""

from __future__ import annotations

from gdmutant.engine.explain import (
    ASSERT_SECTION,
    ENUM_SECTION,
    _block,
    _display_col,
    _enclosing_func,
    context_section,
    doc_url,
    reference_section,
    render_survivor,
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


# --- survivors explained by WHERE they sit ------------------------------------------------------
#
# Some survivors are unkillable by construction, and their operator's advice is not merely unhelpful
# but impossible to follow — which is what makes a whole report read as noise. `context_section`
# names those places; it never claims a mutant is equivalent, and it never suppresses one. Every
# mutant below is still generated, still run, and still counted in the score.

_ASSERT_LINES = ["func f(value):", "\tassert(value > 0)", "\treturn value"]
_ENUM_LINES = ["extends Node", "", "enum Cell { WALL = 0, FLOOR = 1 }", "", "func f():", "\tpass"]
_ENUM_BLOCK = [
    "enum Cell {",
    "\tWALL = 0,",
    "\tFLOOR = 1,",
    "}",
    "",
    "var speed := 1",
]


def test_narrative_switches_to_the_assert_explanation_inside_an_assert() -> None:
    # `>` at column 15 is inside `assert(`, which closes at index 8.
    mutant = _mutant("comparison", ">", ">=", line=2, col=15)
    gap, risk_start = survivor_report_fields(mutant, _ASSERT_LINES)
    assert "sits inside an `assert`" in gap
    assert "no in-process test can kill this one" in gap
    # The operator copy — the advice that cannot be followed here — must be gone, not merely joined.
    assert "equal operands" not in gap + risk_start
    assert "# gdmutant: ignore" in risk_start  # the escape hatch that does apply


def test_narrative_keeps_the_operator_explanation_off_an_assert() -> None:
    mutant = _mutant("comparison", ">", ">=", line=1, col=12)
    gap, _ = survivor_report_fields(mutant, ["\tif value > 0:"])
    assert "equal operands" in gap and "assert" not in gap


def test_deleting_an_assert_statement_is_an_assert_survivor() -> None:
    # Statement deletion replaces the whole `assert(...)` with `pass`. Its token starts *before*
    # the paren, so the column test alone would miss it — the mutant's own text carries the answer.
    deletion = _mutant("statement-deletion", "assert(value > 0)", "pass", line=2, col=2)
    assert reference_section(deletion, _ASSERT_LINES) == ASSERT_SECTION


def test_a_token_before_the_assert_paren_is_not_an_assert_survivor() -> None:
    # Column 5 is inside the leading `if`, ahead of the `assert(` that opens later on the line — so
    # the rule must not claim it. This is what keeps a trailing `# assert(...)` comment harmless.
    lines = ["\tif x > 0: assert(y > 0)"]
    assert context_section(">", 1, 5, lines) is None
    assert context_section(">", 1, 21, lines) == ASSERT_SECTION


def test_the_assert_rule_reaches_a_condition_on_its_own_line() -> None:
    # The common shape once a condition grows: the mutated line is just `a == b`, which says nothing
    # about itself, and the `assert(` is a line above. A per-line rule cannot see this one — and
    # missing it is the costly direction: the reader is handed "add a test with two equal operands"
    # for a check whose failure kills the whole process, i.e. advice that cannot be followed.
    lines = ["func f(a, b):", "\tassert(", "\t\ta == b", "\t)"]
    assert context_section("==", 3, 5, lines) == ASSERT_SECTION
    mutant = _mutant("comparison", "==", "!=", line=3, col=5)
    gap, _ = survivor_report_fields(mutant, lines)
    assert "no in-process test can kill this one" in gap


def test_a_token_after_the_assert_closes_is_not_an_assert_survivor() -> None:
    # Ordinary, killable code sharing the physical line with an assert. Claiming it is the mistake
    # that costs a user a bug: the reader is told "no in-process test can kill this one" about a
    # line an ordinary test kills easily, so the test never gets written.
    lines = ["\tassert(a > b); return c > d"]
    assert context_section(">", 1, 11, lines) == ASSERT_SECTION  # inside the call
    assert context_section(">", 1, 26, lines) is None  # after its closing paren


def test_the_assert_scan_reads_a_continuation_line_end_to_end() -> None:
    # Where a multi-line call's `)` lands is the author's choice — hugging the condition, or alone
    # in the first column, since indentation carries no meaning inside parens. Skipping either end
    # of a continuation line loses the whole call, and a lost call is the silent failure: the reader
    # gets the operator's advice for a check that no in-process test can kill.
    hugging = ["func f(a, b):", "\tassert(", "\t\ta == b)"]
    assert context_section("==", 3, 5, hugging) == ASSERT_SECTION
    alone = ["func f(a, b):", "\tassert(", "\t\ta == b", ")"]
    assert context_section("==", 3, 5, alone) == ASSERT_SECTION


def test_the_assert_rule_starts_and_ends_exactly_at_the_parens() -> None:
    # Both parens are exact boundaries, not approximations. `assert(value > 0)` is the shape asserts
    # usually take, and its last token sits directly against the closing paren — so an off-by-one at
    # either end flips a real verdict on the most ordinary line in the file.
    lines = ["\tassert(value > 0)"]
    assert context_section("0", 1, 17, lines) == ASSERT_SECTION  # the last token inside the call
    assert context_section(">", 1, 8, lines) is None  # the opening paren itself is not inside
    assert context_section(">", 1, 18, lines) is None  # nor is the closing one


def test_a_nested_call_inside_an_assert_does_not_end_it_early() -> None:
    # The assert's own paren closes last, not first. Counting depth is what keeps `len(items)` from
    # being read as the end of the call and dropping the rest of the condition out of the rule.
    lines = ["\tassert(len(items) > 0)"]
    assert context_section(">", 1, 20, lines) == ASSERT_SECTION


def test_an_assert_whose_paren_never_closes_claims_nothing() -> None:
    # An opening paren with no closing one after it anywhere means the scan has misread the file.
    # The honest answer to "I misread this" is to say nothing and let the operator's own narrative
    # stand, rather than declare every mutant below the misread unkillable.
    lines = ["\tassert(", "\tvar n = 1", "\treturn n > 0"]
    assert context_section(">", 3, 11, lines) is None


def test_a_paren_inside_a_string_does_not_hide_the_assert_it_sits_in() -> None:
    # An assert that checks an error message holds a `(` of its own, inside a string. Counting that
    # paren leaves the call looking unclosed, so the scan drops the whole assert and hands the
    # reader "add a test with two equal operands" for a comparison no in-process test can kill.
    # Both quote characters GDScript accepts have to read the same way.
    double = ["func f(x, a, b):", '\tassert(x == "(" and a > b)']
    assert context_section(">", 2, double[1].index(">") + 1, double) == ASSERT_SECTION
    single = ["func f(x, a, b):", "\tassert(x == '(' and a > b)"]
    assert context_section(">", 2, single[1].index(">") + 1, single) == ASSERT_SECTION
    # The whole literal is masked, not just its first character. A message an assert checks reads
    # like prose, so its paren is usually somewhere in the middle rather than up against the quote.
    mid = ["func f(msg, a, b):", '\tassert(msg == "missing (" and a > b)']
    assert context_section(">", 2, mid[1].index(">") + 1, mid) == ASSERT_SECTION


def test_a_backslash_escaped_quote_does_not_end_a_string_early() -> None:
    # GDScript escapes a quote with a backslash. Reading that quote as the end of the string puts
    # the rest of the literal back into the paren count, and the `(` it holds unbalances the call.
    lines = ['\tassert(label == "a\\"(" and a > b)']
    assert context_section(">", 1, lines[0].index(">") + 1, lines) == ASSERT_SECTION


def test_a_paren_inside_a_comment_does_not_end_a_multi_line_assert() -> None:
    # A trailing comment on a continuation line can hold a `)` that belongs to prose, not to code.
    # Counting it satisfies the depth before the real closing paren arrives, so the span stops
    # short and every condition on the lines below falls out of the assert it is genuinely inside.
    lines = [
        "func f(a, b, c, d):",
        "\tassert(",
        "\t\ta > b and  # see note 2)",
        "\t\tc > d",
        "\t)",
    ]
    assert context_section(">", 3, lines[2].index(">") + 1, lines) == ASSERT_SECTION
    assert context_section(">", 4, lines[3].index(">") + 1, lines) == ASSERT_SECTION


def test_a_commented_out_assert_does_not_claim_the_code_below_it() -> None:
    # The mirror of the two cases above. An `assert(` inside a comment opens nothing, so a real
    # closing paren further down cannot be read as its end. Claiming that span is the costly
    # direction: it tells the reader no test can kill a comparison an ordinary test kills easily.
    lines = ["func f(a, b):", "\tvar x = 1  # assert(disabled)", "\treturn foo(a > b)"]
    assert context_section(">", 3, lines[2].index(">") + 1, lines) is None


def test_an_assert_lookalike_identifier_is_not_an_assert() -> None:
    # `my_assert(` and `helper.assert(` are ordinary calls; a failed one raises nothing special, so
    # a mutant inside them is an ordinary, killable survivor.
    assert context_section(">", 1, 16, ["\tmy_assert(a > b)"]) is None
    assert context_section(">", 1, 20, ["\thelper.assert(a > b)"]) is None


def test_narrative_switches_to_the_enum_explanation_on_an_enum_member() -> None:
    # The generic numeric copy tells the reader to "add a test at the boundary this number sets".
    # An enum tag has no boundary, so that advice is not just unhelpful, it is meaningless — and
    # being confidently wrong is what teaches someone to stop reading the report.
    mutant = _mutant("numeric", "0", "1", line=3, col=20)
    gap, risk_start = survivor_report_fields(mutant, _ENUM_LINES)
    assert "changes an `enum` member's value" in gap
    assert "boundary" not in gap + risk_start
    # It says what WOULD make it killable rather than pretending nothing could.
    assert "bitflag" in risk_start and "save file" in risk_start


def test_the_enum_explanation_reaches_a_member_on_its_own_line() -> None:
    # The common shape: the mutated line is just `FLOOR = 1,`, which says nothing about itself. A
    # per-line rule cannot see this one, which is why the scan reads the whole file.
    mutant = _mutant("numeric", "1", "2", line=3, col=10)
    assert reference_section(mutant, _ENUM_BLOCK) == ENUM_SECTION


def test_a_line_after_a_closed_enum_block_is_not_an_enum_member() -> None:
    # `var speed := 1` sits after the enum's `}`. Getting this wrong would silence real numeric
    # advice on ordinary constants, which is the direction that costs a user a bug.
    mutant = _mutant("numeric", "1", "2", line=6, col=15)
    assert reference_section(mutant, _ENUM_BLOCK) == "numeric"


def test_a_line_after_a_single_line_enum_is_not_an_enum_member() -> None:
    mutant = _mutant("numeric", "0", "1", line=6, col=2)
    assert reference_section(mutant, _ENUM_LINES) == "numeric"


def test_the_word_enum_must_open_the_line_to_count() -> None:
    # A string or a comment that merely mentions an enum is not a declaration.
    lines = ['\tvar label = "enum Cell {"', "\tvar n = 1"]
    assert context_section("1", 2, 10, lines) is None


def test_an_unreadable_source_falls_back_to_the_operator_explanation() -> None:
    # With no file there is nothing to read, so the narrative stays the operator's — still accurate,
    # just less specific. It must never guess a context from the operator alone.
    mutant = _mutant("comparison", ">", ">=", line=1, col=15)
    assert context_section(mutant.original, 1, 15, None) is None
    gap, _ = survivor_report_fields(mutant, None)
    assert "equal operands" in gap


def test_a_line_off_the_end_of_the_file_falls_back_too() -> None:
    # A survivor whose file has since shrunk keeps its narrative; only the line-derived detail goes.
    assert context_section(">", 9, 15, ["only one line"]) is None


def test_rendered_survivors_link_to_the_section_that_explains_them() -> None:
    # The `more` link is the one thing a reader clicks. Sending an assert survivor to #comparison
    # would land them on "add a test with two equal operands" — the exact wrong instruction.
    on_assert_mutant = _mutant("comparison", ">", ">=", line=2, col=15)
    block = render_survivor(on_assert_mutant, _ASSERT_LINES)
    assert block[-2] == f"  more   {doc_url(ASSERT_SECTION)}"
    assert "comparison" in block[0]  # the header still names the operator: that IS what changed

    on_enum_mutant = _mutant("numeric", "0", "1", line=3, col=20)
    assert render_survivor(on_enum_mutant, _ENUM_LINES)[-2] == f"  more   {doc_url(ENUM_SECTION)}"
