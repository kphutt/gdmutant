"""Tests for the self-contained HTML report — the view model and the page it renders.

The page's *interactive* behaviour is tested separately, by running the shipped script for real:
see `tests/test_htmlreport_behaviour.py`.
"""

import json
import re
from pathlib import Path
from typing import Any

from gdmutant.engine.explain import DOC_BASE_URL
from gdmutant.engine.htmlreport import (
    FRANK_SVG,
    TAGLINE,
    change_note,
    render_html,
    report_view,
)
from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import stryker_report, stryker_report_multi
from gdmutant.engine.spans import Span

REPO = Path(__file__).resolve().parent.parent


def _mutant(
    line: int,
    col: int,
    end_col: int,
    operator: str,
    replacement: str,
    status: str,
    **extra: str,
) -> dict[str, Any]:
    """One mutant in the report schema — the renderer's real input shape."""
    return {
        "id": f"{line}:{col}:{operator}:{replacement}",
        "mutatorName": operator,
        "replacement": replacement,
        "location": {
            "start": {"line": line, "column": col},
            "end": {"line": line, "column": end_col},
        },
        "status": status,
        **extra,
    }


def _report(source: str, mutants: list[dict[str, Any]], path: str = "a.gd") -> dict[str, Any]:
    return {
        "schemaVersion": "2",
        "files": {path: {"language": "gdscript", "source": source, "mutants": mutants}},
    }


# ---- grouping: findings, not mutants -------------------------------------------------------


def test_mutants_on_one_token_under_one_operator_collapse_into_a_single_finding() -> None:
    # `0 -> 1` and `0 -> -1` are two angles on ONE gap ("this number is not pinned") that one test
    # closes. Reporting them as two findings would duplicate an identical narrative.
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Survived"),
                _mutant(1, 8, 9, "numeric", "-1", "Survived"),
            ],
        )
    )
    (finding,) = view.files[0].findings
    assert finding.op == "numeric"
    assert [a.change for a in finding.angles] == ["Changed 0 to 1", "Changed 0 to -1"]
    # The header still counts mutants — only the work list is grouped.
    assert view.total == 2
    assert view.survived == 2


def test_overlapping_spans_under_different_operators_stay_separate_findings() -> None:
    # On `return 0` the `numeric` span sits INSIDE the `statement-deletion` span, but they are
    # genuinely different gaps: the value is not pinned / the whole line could vanish unnoticed.
    # Grouping across operators would merge two unrelated pieces of work.
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 1, 9, "statement-deletion", "pass", "Survived"),
                _mutant(1, 8, 9, "numeric", "1", "Survived"),
            ],
        )
    )
    assert [f.op for f in view.files[0].findings] == ["statement-deletion", "numeric"]


def test_the_same_operator_on_a_different_token_is_a_different_finding() -> None:
    view = report_view(
        _report(
            "return a > b and c > d\n",
            [
                _mutant(1, 10, 11, "comparison", ">=", "Survived"),
                _mutant(1, 20, 21, "comparison", ">=", "Survived"),
            ],
        )
    )
    assert [f.col for f in view.files[0].findings] == [10, 20]


def test_finding_ids_are_unique_across_files() -> None:
    report = _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")])
    report["files"]["b.gd"] = {
        "language": "gdscript",
        "source": "return 0\n",
        "mutants": [_mutant(1, 8, 9, "numeric", "1", "Survived")],
    }
    ids = [f.fid for file in report_view(report).files for f in file.findings]
    assert len(ids) == len(set(ids)) == 2


# ---- verdict roll-up -------------------------------------------------------------------------


def test_any_surviving_angle_makes_the_whole_finding_actionable() -> None:
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Killed"),
                _mutant(1, 8, 9, "numeric", "-1", "Survived"),
            ],
        )
    )
    (finding,) = view.files[0].findings
    assert finding.cls == "sv"
    # Angles that disagree are labelled as such rather than flattened to one of them.
    assert finding.tag == "mixed"


def test_a_finding_every_test_caught_is_green_and_one_never_run_is_neither() -> None:
    view = report_view(
        _report(
            "return a > b\n",
            [
                _mutant(1, 10, 11, "comparison", ">=", "Killed"),
                _mutant(1, 10, 11, "comparison", "<", "Timeout"),
                _mutant(2, 1, 2, "constant", "false", "CompileError"),
            ],
            path="a.gd",
        )
    )
    caught, never_run = view.files[0].findings
    assert (caught.cls, caught.tag) == ("kd", "caught")
    # Never green: green would claim a test caught something that never even parsed.
    assert (never_run.cls, never_run.tag) == ("ot", "invalid")


def test_the_narrative_comes_from_the_first_angle_that_has_one() -> None:
    # Only survivors carry the narrative. Taking it from the first angle unconditionally lost the
    # copy whenever a killed mutant sorted ahead of a surviving one on the same token, leaving a
    # finding tagged SURVIVED with nothing to say.
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Killed"),
                _mutant(
                    1,
                    8,
                    9,
                    "numeric",
                    "-1",
                    "Survived",
                    description="the gap",
                    statusReason="the risk\n\nthe fix",
                ),
            ],
        )
    )
    (finding,) = view.files[0].findings
    assert (finding.gap, finding.risk, finding.fix) == ("the gap", "the risk", "the fix")


def test_a_report_without_the_narrative_fields_still_renders() -> None:
    # Older reports (and any third-party producer) carry only id/mutatorName/replacement/location/
    # status. The narrative blocks must simply not render, never raise.
    view = report_view(_report("return 0\n", [{**_mutant(1, 8, 9, "numeric", "1", "Survived")}]))
    (finding,) = view.files[0].findings
    assert (finding.gap, finding.risk, finding.fix) == ("", "", "")
    assert "<div class=" in render_html(
        _report("return 0\n", [_mutant(1, 8, 9, "n", "1", "Survived")])
    )


# ---- tallies ---------------------------------------------------------------------------------


def test_the_score_matches_the_run_it_came_from() -> None:
    # The page recomputes the score from statuses; it must land on the same number
    # `MutationRun.mutation_score` reports, or the report and the console disagree.
    run = MutationRun(
        (
            MutantOutcome(Mutant("a.gd", Span(1, 8, 1, 9), "numeric", "0", "1"), Verdict.KILLED),
            MutantOutcome(Mutant("a.gd", Span(1, 8, 1, 9), "numeric", "0", "2"), Verdict.TIMEOUT),
            MutantOutcome(Mutant("a.gd", Span(2, 8, 2, 9), "numeric", "0", "3"), Verdict.SURVIVED),
            MutantOutcome(Mutant("a.gd", Span(3, 8, 3, 9), "numeric", "0", "4"), Verdict.ERROR),
        )
    )
    view = report_view(stryker_report(run, "a.gd", "return 0\n" * 3, "gdscript"))
    assert run.mutation_score is not None
    assert view.score == round(run.mutation_score * 100, 1)
    assert (view.detected, view.survived, view.total) == (2, 1, 4)


def test_a_run_with_nothing_killable_scores_none_rather_than_zero() -> None:
    view = report_view(_report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "CompileError")]))
    assert view.score is None
    assert "no mutants could be scored" in render_html(
        _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "CompileError")])
    )


def test_rare_statuses_appear_only_when_they_happened() -> None:
    plain = report_view(_report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")]))
    assert plain.rare == []
    mixed = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Ignored"),
                _mutant(2, 8, 9, "numeric", "1", "RuntimeError"),
            ],
        )
    )
    assert mixed.rare == [("ignored", 1), ("runtime errors", 1)]


def test_operator_chips_count_findings_of_that_file() -> None:
    view = report_view(
        _report(
            "return a > b\nreturn 0\n",
            [
                _mutant(1, 10, 11, "comparison", ">=", "Survived"),
                _mutant(2, 8, 9, "numeric", "1", "Survived"),
                _mutant(2, 8, 9, "numeric", "-1", "Survived"),
            ],
        )
    )
    # Two numeric MUTANTS are one numeric FINDING — the chip counts what the stepper walks.
    assert view.files[0].ops == [("comparison", 1), ("numeric", 1)]


def test_files_are_ordered_by_survivors_so_the_index_opens_on_the_worst() -> None:
    runs = {
        "quiet.gd": (
            MutationRun(
                (
                    MutantOutcome(
                        Mutant("quiet.gd", Span(1, 8, 1, 9), "numeric", "0", "1"), Verdict.KILLED
                    ),
                )
            ),
            "return 0\n",
        ),
        "loud.gd": (
            MutationRun(
                (
                    MutantOutcome(
                        Mutant("loud.gd", Span(1, 8, 1, 9), "numeric", "0", "1"), Verdict.SURVIVED
                    ),
                    MutantOutcome(
                        Mutant("loud.gd", Span(2, 8, 2, 9), "numeric", "0", "1"), Verdict.SURVIVED
                    ),
                )
            ),
            "return 0\nreturn 0\n",
        ),
    }
    view = report_view(stryker_report_multi(runs, "gdscript"))
    assert [f.path for f in view.files] == ["loud.gd", "quiet.gd"]
    assert [f.survived for f in view.files] == [2, 0]


# ---- coordinates -----------------------------------------------------------------------------


def test_columns_are_expanded_through_tabs_so_the_marker_lands_on_the_token() -> None:
    # GDScript indents with tabs; the page draws them expanded to four spaces. A column counted
    # against the raw line would put the marker three characters left per leading tab.
    view = report_view(
        _report(
            "func f():\n\t\treturn a > b\n", [_mutant(2, 12, 13, "comparison", ">=", "Survived")]
        )
    )
    (finding,) = view.files[0].findings
    assert finding.col == 18 and finding.colEnd == 19
    assert view.files[0].lines[1] == "        return a > b"
    # …and the marked slice is the operator itself, not its neighbour.
    assert view.files[0].lines[1][finding.col - 1 : finding.colEnd - 1] == ">"


def test_the_enclosing_function_is_named() -> None:
    view = report_view(
        _report(
            "static func ties_favor_earlier():\n\treturn true\n",
            [_mutant(2, 9, 13, "constant", "false", "Survived")],
        )
    )
    assert view.files[0].findings[0].func == "ties_favor_earlier"


def test_a_mutant_off_the_end_of_the_source_does_not_crash_the_view() -> None:
    view = report_view(_report("return 0\n", [_mutant(99, 1, 2, "numeric", "1", "Survived")]))
    assert view.files[0].findings[0].line == 99


# ---- change notes ----------------------------------------------------------------------------


def test_change_note_phrases_deletions_as_removals() -> None:
    # Deriving the phrasing from the replacement alone produced "replaced it with pass" for a
    # deleted line and a dangling "replaced it with " for a dropped `not`.
    assert change_note("statement-deletion", "return 0", "pass") == "This whole line was removed"
    assert change_note("logical-not", "not ", "") == "Removed not "
    assert change_note("comparison", ">", ">=") == "Changed > to >="


# ---- the page --------------------------------------------------------------------------------


def _page() -> str:
    return render_html(
        _report(
            "func f():\n\treturn a > b\n",
            [
                _mutant(
                    2,
                    11,
                    12,
                    "comparison",
                    ">=",
                    "Survived",
                    description="the gap",
                    statusReason="the risk\n\nthe fix",
                )
            ],
        )
    )


def test_the_page_fetches_nothing_it_needs_to_render() -> None:
    # The whole point: no CDN, no fonts, no images, no XHR. A report that goes blank without a
    # network cannot be a CI artifact, an email attachment, or an offline read.
    page = _page()
    for attribute in re.findall(r'\b(?:src|href)\s*=\s*"([^"]*)"', page):
        assert not attribute.startswith(("http://", "https://", "//")), attribute
    assert "<link" not in page
    assert "@import" not in page
    assert "url(http" not in page
    for banned in ("fetch(", "XMLHttpRequest", "importScripts", "WebSocket", "unpkg"):
        assert banned not in page, banned


def test_the_only_external_links_are_documentation_a_reader_may_click() -> None:
    # Anchors to the operator reference are fine — they are an offer, not a dependency. Pin that
    # they are the *only* absolute URLs, so a resource can never sneak in as one.
    page = render_html(
        _report(
            "return a > b\n",
            [_mutant(1, 10, 11, "comparison", ">=", "Survived")],
        )
    )
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", page))
    assert urls == {"http://www.w3.org/2000/svg", DOC_BASE_URL}


def test_frank_and_the_tagline_ride_along_in_the_page() -> None:
    page = _page()
    assert FRANK_SVG in page
    assert TAGLINE in page
    # Inlined, not linked: a URL would put the page back on the network.
    assert "frank.svg" not in page


def test_frank_matches_the_repo_asset_he_was_traced_from() -> None:
    # The shipped copy is a hand-inlined form of `.github/assets/frank.svg` (which ships in no
    # distribution). Pin every drawing instruction so a redraw of the mascot cannot leave the
    # report showing the old one.
    asset = (REPO / ".github/assets/frank.svg").read_text(encoding="utf-8")
    shapes = re.findall(r"<(?:rect|circle|path)[^>]*>", asset)
    assert shapes, "the asset should still be a plain shape list"
    for shape in shapes:
        normalized = re.sub(r"\s+", " ", shape).replace(" />", "/>")
        assert normalized in FRANK_SVG, shape


def test_the_report_json_is_embedded_and_parses_back_exactly() -> None:
    # Genuinely useful for other tooling, so it survives the move off the stock viewer even though
    # nothing in the page reads it back.
    report = _report("return a > b\n", [_mutant(1, 10, 11, "comparison", ">=", "Survived")])
    page = render_html(report)
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block) == report


def test_source_containing_a_script_close_cannot_break_out_of_either_data_block() -> None:
    # GDScript source containing `</script>` must not close the data block or the renderer's own
    # script early. It is escaped to `<\/script>` — valid JSON that round-trips on parse.
    report = _report('var s := "</script><img src=x>"', [])
    page = render_html(report)
    assert "</script><img" not in page
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block)["files"]["a.gd"]["source"] == 'var s := "</script><img src=x>"'


def test_the_inlined_reference_carries_only_the_operators_this_report_used() -> None:
    view = report_view(
        _report("return a > b\n", [_mutant(1, 10, 11, "comparison", ">=", "Survived")])
    )
    assert set(view.refs) == {"comparison"}
    # …rendered from the page's two inline markers into real markup.
    labels = [label for label, _ in view.refs["comparison"]]
    assert labels == ["The change", "Why it survived", "How to kill it", "Equivalent mutant?"]
    assert "<code>&gt;</code>" in view.refs["comparison"][0][1]
    assert "<strong>equal</strong>" in view.refs["comparison"][1][1]


def test_an_empty_report_still_renders_a_page() -> None:
    page = render_html({"schemaVersion": "2", "files": {}})
    assert "<html" in page and "no mutants could be scored" in page
