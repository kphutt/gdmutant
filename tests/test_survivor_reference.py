"""The inlined operator reference must stay identical to the docs page it was taken from.

`gdmutant/engine/survivor_reference.py` ships the reference inside the wheel, because `docs/` does
not ship and the HTML report has to explain a survivor with no network. That makes two copies of
the same prose, so this test is the thing that keeps them one fact: edit
`docs/survivors/README.md` and the suite fails, pointing at the constant to update.
"""

import re
from pathlib import Path

from gdmutant.engine.explain import doc_url
from gdmutant.engine.survivor_reference import SURVIVOR_REFERENCE

PAGE = Path(__file__).resolve().parent.parent / "docs" / "survivors" / "README.md"


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
