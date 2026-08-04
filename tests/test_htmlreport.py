"""Tests for the self-contained HTML report — the view model and the page it renders.

The page's *interactive* behaviour is tested separately, by running the shipped script for real:
see `tests/test_htmlreport_behaviour.py`.
"""

import base64
import json
import re
from pathlib import Path
from typing import Any

from gdmutant.engine.explain import DOC_BASE_URL
from gdmutant.engine.htmlreport import (
    FRANK_SVG,
    TAGLINE,
    _render_inline_markdown,
    change_note,
    finding_key,
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


def test_finding_keys_are_unique_across_files() -> None:
    # `fid` is only unique WITHIN a file — two files can hold the identical token under the
    # identical operator, and do here. The path is what separates them, which is why the address
    # the page links to is the joined key, never the bare id.
    report = _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")])
    report["files"]["b.gd"] = {
        "language": "gdscript",
        "source": "return 0\n",
        "mutants": [_mutant(1, 8, 9, "numeric", "1", "Survived")],
    }
    view = report_view(report)
    assert {f.fid for file in view.files for f in file.findings} == {"1:8:9:numeric"}
    keys = [finding_key(file.path, f.fid) for file in view.files for f in file.findings]
    assert sorted(keys) == ["a.gd:1:8:9:numeric", "b.gd:1:8:9:numeric"]


def test_a_findings_id_is_the_tuple_it_was_grouped_by_so_it_cannot_collide() -> None:
    # Co-located findings of DIFFERENT operators are the collision that would matter: on `return 0`
    # the `numeric` span sits inside the `statement-deletion` span. The operator is in the id, so
    # they stay two addresses and never collide.
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Survived"),
                _mutant(1, 1, 9, "statement-deletion", "pass", "Survived"),
            ],
        )
    )
    assert [f.fid for f in view.files[0].findings] == [
        "1:8:9:numeric",
        "1:1:9:statement-deletion",
    ]


def test_a_finding_keeps_its_id_when_the_run_is_repeated_and_the_source_has_not_moved() -> None:
    # The whole point of the identity: regenerate, and every link still lands. The outcome may
    # change — a test now kills it — without the address changing.
    before = report_view(_report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")]))
    after = report_view(_report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Killed")]))
    assert before.files[0].findings[0].fid == after.files[0].findings[0].fid


def test_render_html_is_deterministic_for_an_unchanged_report() -> None:
    report = _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")])
    assert render_html(report) == render_html(report)


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
                    statusReason="the risk\n\nthe start",
                ),
            ],
        )
    )
    (finding,) = view.files[0].findings
    assert (finding.gap, finding.risk, finding.start) == ("the gap", "the risk", "the start")


def test_a_report_without_the_narrative_fields_still_renders() -> None:
    # Older reports (and any third-party producer) carry only id/mutatorName/replacement/location/
    # status. The narrative blocks must simply not render, never raise.
    view = report_view(_report("return 0\n", [{**_mutant(1, 8, 9, "numeric", "1", "Survived")}]))
    (finding,) = view.files[0].findings
    assert (finding.gap, finding.risk, finding.start) == ("", "", "")
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
    # The status rides along with the label because the header renders each count as a filter
    # button, and a button has to name what it filters on.
    assert mixed.rare == [("ignored", 1, "Ignored"), ("runtime errors", 1, "RuntimeError")]


def test_a_finding_records_which_rare_statuses_it_holds_so_a_header_count_can_reach_it() -> None:
    # A header count is only a number until the reader can get to the mutants behind it. This is
    # what the click filters on, and the three states must stay distinguishable: a timeout is a
    # kill, a compile error never ran, and a runtime error ran and measured nothing.
    view = report_view(
        _report(
            "return a > b\nreturn 0\nreturn 1\n",
            [
                _mutant(1, 10, 11, "comparison", ">=", "Timeout"),
                _mutant(2, 8, 9, "numeric", "1", "RuntimeError"),
                _mutant(3, 8, 9, "numeric", "1", "Survived"),
            ],
        )
    )
    assert [f.rare for f in view.files[0].findings] == [["Timeout"], ["RuntimeError"], []]


def test_a_findings_rare_statuses_are_deduped_and_keep_the_mutants_own_order() -> None:
    # Two runtime errors on one token are one entry, not two. The list answers "does this finding
    # hold one?", and a stable order is what keeps the rendered page byte-identical run to run.
    view = report_view(
        _report(
            "return 0\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "RuntimeError"),
                _mutant(1, 8, 9, "numeric", "-1", "Timeout"),
                _mutant(1, 8, 9, "numeric", "2", "RuntimeError"),
            ],
        )
    )
    (finding,) = view.files[0].findings
    assert finding.rare == ["RuntimeError", "Timeout"]


def test_the_header_renders_each_rare_count_as_a_filter_and_the_common_ones_as_text() -> None:
    # Only the rare counts are clickable: they were the numbers with nothing behind them.
    page = render_html(
        _report(
            "return 0\nreturn 1\n",
            [
                _mutant(1, 8, 9, "numeric", "1", "Survived"),
                _mutant(2, 8, 9, "numeric", "1", "RuntimeError"),
            ],
        )
    )
    head = page.split('<div class="head"', 1)[1].split("</div>\n  <div id=", 1)[0]
    assert 'data-filter="rare:RuntimeError"' in head
    assert '<div class="stat sv"><b>1</b><i>survived</i></div>' in head
    assert head.count("<button") == 1  # the one rare status this run produced, and nothing else


# ---- paths a reader can act on -----------------------------------------------------------------


def test_a_file_inside_the_project_is_shown_by_its_project_relative_path(tmp_path: Path) -> None:
    # The report is made to travel. An absolute path is the author's own machine, their username
    # and their directory layout, in every row of an artifact meant to be mailed, and it is noise
    # to every reader but the one who made it. It also puts ~60 identical characters in front of
    # the only part that distinguishes one row from the next, in the one column a reader scans.
    source = tmp_path / "src" / "core" / "diff.gd"
    source.parent.mkdir(parents=True)
    source.write_text("return 0\n", encoding="utf-8")
    view = report_view(
        _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")], path=str(source)),
        str(tmp_path),
    )
    assert view.files[0].path == "src/core/diff.gd"


def test_a_file_outside_the_project_keeps_its_absolute_path(tmp_path: Path) -> None:
    # There is no shorter honest name for it, and a `..`-laden relative path would be worse on both
    # counts. The fallback is stated rather than assumed, because a silently absolute row in an
    # otherwise relative index is exactly the thing a reader would misread.
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "elsewhere" / "stray.gd"
    outside.parent.mkdir()
    view = report_view(
        _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")], path=str(outside)),
        str(project),
    )
    # `as_posix`, never a hand-written separator: the page draws every path with forward slashes so
    # a Windows run and a POSIX one of the same project agree, and the expectation has to say that
    # rather than depend on which platform runs the test.
    assert view.files[0].path == outside.as_posix()


def test_without_a_project_root_the_path_is_shown_exactly_as_the_report_keys_it() -> None:
    # A report rendered by something other than the CLI has no root to shorten against, and
    # inventing one from the current directory would produce a path relative to wherever the
    # renderer happened to run.
    view = report_view(_report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")]))
    assert view.files[0].path == "a.gd"


def test_the_displayed_path_is_what_findings_are_addressed_by(tmp_path: Path) -> None:
    # The path is not decoration: it is half of a finding's address, so it is what a deep link is
    # keyed on. Shortening it in the index and addressing by something else would give the page two
    # names for one file.
    source = tmp_path / "src" / "diff.gd"
    source.parent.mkdir()
    report = _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")], path=str(source))
    page = render_html(report, str(tmp_path))
    assert '"path": "src/diff.gd"' in page
    # …and nothing the reader can see carries the absolute one. The data block is the deliberate
    # exception, pinned by the test below, so the check stops where it starts.
    visible = page.split('<script type="application/json"', 1)[0]
    assert str(tmp_path).replace("\\", "/") not in visible


def test_the_embedded_json_still_carries_the_paths_the_run_was_given(tmp_path: Path) -> None:
    # The display is what changed; the data block is not. Its keys are the report's identifiers and
    # other tooling resolves them, so they stay exactly what the run was handed. Pinned here so the
    # split is deliberate and visible rather than a surprise to whoever parses the block.
    source = tmp_path / "src" / "diff.gd"
    source.parent.mkdir()
    report = _report("return 0\n", [_mutant(1, 8, 9, "numeric", "1", "Survived")], path=str(source))
    page = render_html(report, str(tmp_path))
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block) == report


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
    # The page carries exactly one <link>: the favicon, inlined as a `data:` URI. The old guard —
    # "no <link> at all" — only ever stood in for the real rule, which is "no <link> that fetches".
    links = re.findall(r"<link\b[^>]*>", page)
    assert len(links) == 1, links
    assert 'href="data:image/svg+xml;base64,' in links[0], links[0]
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


def test_the_tab_icon_is_frank_himself_inlined_and_decodes_back_to_the_same_markup() -> None:
    # A `data:` URI favicon fails SILENTLY when it is escaped wrongly, and Frank is full of `#`
    # colour literals — a bare `#` there starts the URI's fragment and the browser gets a truncated
    # SVG, with the markup still looking perfectly correct. Base64 removes that class of bug, and
    # this decodes it back to prove the round trip rather than trusting the tag's shape.
    page = _page()
    (href,) = re.findall(r'<link rel="icon" href="([^"]+)"', page)
    prefix = "data:image/svg+xml;base64,"
    assert href.startswith(prefix)
    assert base64.b64decode(href[len(prefix) :]).decode("utf-8") == FRANK_SVG
    # Small enough that every report can carry it without thinking about it.
    assert len(href) < 2048, len(href)


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
    # Genuinely useful for other tooling, so it survives the move off the stock viewer.
    report = _report("return a > b\n", [_mutant(1, 10, 11, "comparison", ">=", "Survived")])
    page = render_html(report)
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block) == report


def test_the_embedded_report_is_offered_as_a_download_without_leaving_the_page() -> None:
    # It had been in every report from the first one, and nothing said so: the only way to reach it
    # was View Source. The button adds no data and no request. The bytes are the page's own and
    # the `blob:` URL is minted in the browser from them, so self-containment is untouched.
    page = _page()
    assert 'id="dl"' in page
    script = page.rsplit("<script>", 1)[1]
    assert "URL.createObjectURL" in script and "new Blob(" in script
    assert "$('#mutation-test-report').textContent" in script
    # A fixed name. Naming the file after the run's own path would put somebody's directory layout
    # back into an artifact that just stopped carrying it.
    assert "a.download = 'gdmutant-report.json'" in script


def test_source_containing_a_script_close_cannot_break_out_of_either_data_block() -> None:
    # GDScript source containing `</script>` must not close the data block or the renderer's own
    # script early. It is escaped to `<\/script>` — valid JSON that round-trips on parse.
    report = _report('var s := "</script><img src=x>"', [])
    page = render_html(report)
    assert "</script><img" not in page
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block)["files"]["a.gd"]["source"] == 'var s := "</script><img src=x>"'


def test_source_containing_a_double_escape_sequence_cannot_blank_the_report() -> None:
    # `<!--<script` needs no `/` at all: it is the exact sequence that pushes an HTML tokenizer
    # into "script data double escaped" state, where the page's own real `</script>` closing tag
    # is then read as literal text instead of a tag, and everything after it in the document goes
    # unrendered. Escaping only `</` (the earlier fix) let this one through.
    report = _report('var s := "<!--<script>x</script>-->"', [])
    page = render_html(report)
    assert "<!--<script" not in page
    assert "<" not in page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    block = page.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block)["files"]["a.gd"]["source"] == 'var s := "<!--<script>x</script>-->"'


def test_the_inlined_reference_carries_only_the_operators_this_report_used() -> None:
    view = report_view(
        _report("return a > b\n", [_mutant(1, 10, 11, "comparison", ">=", "Survived")])
    )
    assert set(view.refs) == {"comparison"}
    # …rendered from the page's inline markers into real markup.
    labels = [label for label, _ in view.refs["comparison"]]
    assert labels == ["The change", "Why it survived", "How to kill it", "Equivalent mutant?"]
    assert "<code>&gt;</code>" in view.refs["comparison"][0][1]


def test_the_reference_renderer_escapes_then_re_applies_both_inline_markers() -> None:
    # The reference page uses backticks throughout and, since the house-style pass, no bold — so
    # the bold branch has no live example left to assert against in the test above. It stays
    # supported and pinned here instead, so a page edit that reintroduces bold renders as markup
    # rather than leaking literal asterisks into the report.
    assert _render_inline_markdown("a `x` b") == "a <code>x</code> b"
    assert _render_inline_markdown("a **x** b") == "a <strong>x</strong> b"
    # Escaping happens first, so source characters can never become markup.
    assert _render_inline_markdown("<b> & `x`") == "&lt;b&gt; &amp; <code>x</code>"


def test_an_assert_survivor_expands_the_assert_reference_not_its_operators() -> None:
    # The page reads its narrative back out of the report, so an assert survivor already *says*
    # "no in-process test can kill this one". If the expandable reference beside it still offered
    # the comparison entry ("add a test with two equal operands"), the page would contradict itself
    # in adjacent paragraphs. `ref` is what keeps the two halves agreeing.
    view = report_view(
        _report(
            "assert(a > b)\n",
            [_mutant(1, 10, 11, "comparison", ">=", "Survived")],
        )
    )
    finding = view.files[0].findings[0]
    assert finding.op == "comparison"  # still what changed — the operator chip is unaffected
    assert finding.ref == "assert"  # …but the explanation and the link are the assert one
    assert set(view.refs) == {"assert"}


def test_a_survivor_off_an_assert_keeps_its_operator_reference() -> None:
    view = report_view(
        _report("return a > b\n", [_mutant(1, 10, 11, "comparison", ">=", "Survived")])
    )
    assert view.files[0].findings[0].ref == "comparison"


def test_an_empty_report_still_renders_a_page() -> None:
    page = render_html({"schemaVersion": "2", "files": {}})
    assert "<html" in page and "no mutants could be scored" in page
