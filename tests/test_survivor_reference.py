"""The inlined operator reference must stay identical to the docs page it was taken from.

`gdmutant/engine/survivor_reference.py` ships the reference inside the wheel, because `docs/` does
not ship and the HTML report has to explain a survivor with no network. That makes two copies of
the same prose, so this test is the thing that keeps them one fact: edit
`docs/survivors/README.md` and the suite fails, pointing at the constant to update.
"""

import re
from pathlib import Path

from gdmutant.adapters.gdscript import find_sites
from gdmutant.engine.explain import ENUM_SECTION, context_section, doc_url
from gdmutant.engine.operators import COMPARISON, NUMERIC
from gdmutant.engine.survivor_reference import SURVIVOR_REFERENCE

PAGE = Path(__file__).resolve().parent.parent / "docs" / "survivors" / "README.md"


def _body(operator: str, label: str) -> str:
    """One labelled paragraph of an entry — the text a reader actually acts on."""
    return dict(SURVIVOR_REFERENCE[operator])[label]


def _sections_from_the_page() -> dict[str, list[tuple[str, str]]]:
    """Parse the docs page into ``{operator id: [(label, body), …]}``.

    The page's per-operator entries are `## Heading` sections whose body is a run of `**Label**`
    paragraphs. Parsing lives here, in the test, rather than in shipped code: a regex over prose is
    exactly the fragile thing that belongs where a failure is loud and the fix obvious.
    """
    text = re.sub(r"^---.*?---\s*", "", PAGE.read_text(encoding="utf-8"), flags=re.S)
    parts = re.split(r"^##\s+(.+)$", text, flags=re.M)
    out: dict[str, list[tuple[str, str]]] = {}
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        # GitHub's heading slug, which is what `doc_url`'s anchor resolves against.
        slug = re.sub(r"[^a-z0-9 -]", "", heading.strip().lower()).replace(" ", "-")
        sections = []
        for line in body.splitlines():
            # A label is bold at the START of a line. Bold mid-sentence is emphasis in the body and
            # must not be mistaken for one. Trailing punctuation is optional because the labels are
            # "The change:" but also "Equivalent mutant?" — requiring a colon silently dropped the
            # equivalence section, which is the triage signal.
            match = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", line.strip())
            if match:
                sections.append((match.group(1).rstrip(":"), match.group(2)))
        if sections:
            out[slug] = sections
    return out


def test_every_operator_section_matches_the_docs_page_word_for_word() -> None:
    assert {k: tuple(v) for k, v in _sections_from_the_page().items()} == SURVIVOR_REFERENCE


def test_each_key_is_the_anchor_the_more_link_points_at() -> None:
    # The report's inline expansion and the console's `more` link must resolve to the same section,
    # not two parallel contracts that can drift.
    for operator in SURVIVOR_REFERENCE:
        assert doc_url(operator).endswith(f"#{operator}")


def test_every_entry_carries_the_equivalent_mutant_triage_signal() -> None:
    # "Is this survivor legitimate?" is the question a reader has once they understand the change;
    # an entry missing it sends them to a browser tab the offline report exists to avoid.
    for operator, sections in SURVIVOR_REFERENCE.items():
        labels = [label for label, _ in sections]
        assert "Equivalent mutant?" in labels, operator
        assert all(body for _, body in sections), operator


# The tests above pin the two copies of the reference to each other. Passing them proves only that
# the page and the shipped constant say the same thing, never that the thing is true. The tests
# below check the claims themselves against the code that generates the mutants, so a wrong
# explanation cannot stay synchronised into every report.


def test_the_modulo_entry_blames_the_missing_assertion_not_a_clean_multiple() -> None:
    # The old text said a modulo mutant survives because "every test input is a clean multiple,
    # where `%`, `*`, and `/` can produce indistinguishable results". Under a clean multiple those
    # operators are as distinguishable as they ever get: 6 % 3 is 0, 6 * 3 is 18, 6 / 3 is 2. The
    # reader was told the test that works (a clean multiple plus a real assertion) cannot work.
    assert 6 % 3 != 6 * 3 and 6 % 3 != 6 / 3
    body = _body("modulo", "Why it survived")
    assert "nothing pins the exact result" in body  # arithmetic's reason, which is the real one
    assert "clean multiple" in body and "not the cause" in body  # and the old claim, retracted


def test_the_comparison_entry_covers_the_equality_swaps_it_is_used_for() -> None:
    # `==` and `!=` are exact complements, so they differ on EVERY input, not at a boundary. Both
    # are in the operator's table and `==` is named in the entry's own "The change" line, so an
    # entry that explains only `>` against `>=` misdiagnoses half of what it covers.
    assert COMPARISON.replacements("==") == ("!=",) and COMPARISON.replacements("!=") == ("==",)
    body = _body("comparison", "Why it survived")
    assert "`==`" in body and "`!=`" in body


def test_the_numeric_entry_says_integer_because_that_is_all_that_is_mutated() -> None:
    # A reader with a float bound would otherwise believe it is covered.
    for literal in ("0.5", "2.5", "0xFF", "1_000"):
        assert NUMERIC.replacements(literal) == (), literal
    body = _body("numeric", "The change")
    assert "integer literal" in body


def test_the_enum_entry_is_as_wide_as_the_router_that_sends_mutants_to_it() -> None:
    # `context_section` routes any mutant whose line falls inside an `enum { }` block here, not
    # only the `numeric` ones, so an arithmetic mutant on a computed member reads this entry too.
    assert context_section("+", 2, 8, ["enum Cell {", "\tA = 1 + 0,", "}"]) == ENUM_SECTION
    assert "arithmetic" in _body("enum-member", "The change")


def test_the_compound_assign_entry_does_not_claim_a_string_line_is_untouched() -> None:
    # The compound-assign operator skips a string `+=`, but the line still gets a statement
    # deletion, so "not mutated at all" said more than was true.
    assert "not mutated at all" not in _body("compound-assign", "The change")
    assert "statement-deletion" in _body("compound-assign", "The change")


def test_the_exclusions_umbrella_describes_the_inert_shape_as_well_as_the_invalid_ones() -> None:
    # Three excluded shapes rest on `String` having no `-`, so their mutant is code GDScript
    # rejects. The fourth, a property initializer no getter can read back, is valid GDScript that
    # merely stores a value nothing reads: it parses (find_sites parses before it selects), so a
    # reader auditing the exclusions was sent looking for a syntax error that is not there.
    dead_initializer_mutant = "var m = 31 :\n\tget:\n\t\treturn 5\n"
    assert [site.token for site in find_sites(dead_initializer_mutant)] == ["5"]
    umbrella = PAGE.read_text(encoding="utf-8").split("### What is never generated")[1]
    assert "inert" in umbrella.split("\n## ")[0]
