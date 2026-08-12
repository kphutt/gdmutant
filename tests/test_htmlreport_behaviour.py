"""Run the report page's real inlined script, and assert on what it does.

The page's triage loop is behaviour, not markup: stepping through findings, changing filters,
reaching a finding from the keyboard. Asserting on the generated HTML would only re-state the
template. So `tests/js/harness.js` executes the shipped script against a recording DOM stand-in and
prints what the page displayed; this module drives that and checks it.

Skipped when `node` is not on PATH — the rest of the suite (and the whole CLI) stays Node-free.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gdmutant.engine.htmlreport import finding_key, render_html, report_view

HARNESS = Path(__file__).resolve().parent / "js" / "harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_SOURCE = "func f(a, b):\n\tif a > b and a < b:\n\t\treturn 0\n\treturn a + b\n"


def _mutant(
    line: int, col: int, end_col: int, operator: str, replacement: str, status: str
) -> dict[str, Any]:
    return {
        "id": f"{line}:{col}:{replacement}",
        "mutatorName": operator,
        "replacement": replacement,
        "location": {
            "start": {"line": line, "column": col},
            "end": {"line": line, "column": end_col},
        },
        "status": status,
        "description": "the gap",
        "statusReason": "the risk\n\nthe fix",
    }


#: Six findings from seven mutants: four survived, two caught. The two `numeric` mutants on line 3
#: share a token AND an operator, so they collapse into one finding — which is exactly the
#: mutants-vs-findings gap the stepper has to walk correctly.
_MUTANTS = [
    _mutant(2, 5, 6, "comparison", ">=", "Survived"),
    _mutant(2, 11, 14, "boolean", "or", "Survived"),
    _mutant(2, 17, 18, "comparison", "<=", "Killed"),
    _mutant(3, 10, 11, "numeric", "1", "Survived"),
    _mutant(3, 10, 11, "numeric", "-1", "Survived"),
    _mutant(3, 3, 11, "statement-deletion", "pass", "Survived"),
    _mutant(4, 11, 12, "arithmetic", "-", "Killed"),
]

_OPS = ["arithmetic", "boolean", "comparison", "numeric", "statement-deletion"]


def _report(mutants: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "2",
        "files": {"a.gd": {"language": "gdscript", "source": _SOURCE, "mutants": mutants}},
    }


#: More files, so the run opens on the file index, the only place the index rows, the "all files"
#: button and the sortable column headings exist to be clicked at all.
#:
#: The three are shaped so that every column sorts them into a different order, which is the only
#: way a test can tell a working sort from a list that never moved:
#:
#:   ============ ========= ======= ======= =====
#:   file         survived  caught  mutants score
#:   ============ ========= ======= ======= =====
#:   ``a.gd``     5         2       7       28.6
#:   ``b.gd``     1         0       1       0.0
#:   ``c.gd``     0         3       3       100.0
#:   ============ ========= ======= ======= =====
#:
#: ``c.gd`` also carries the run's only ``Timeout``, so the header's rare-status count has exactly
#: one file to reach, and it is not the file the index opens on, which is what makes "the click
#: went somewhere" a real observation.
def _multi_report() -> dict[str, Any]:
    report = _report(_MUTANTS)
    report["files"]["b.gd"] = {
        "language": "gdscript",
        "source": _SOURCE,
        "mutants": [_mutant(2, 5, 6, "comparison", ">=", "Survived")],
    }
    report["files"]["c.gd"] = {
        "language": "gdscript",
        "source": _SOURCE,
        "mutants": [
            _mutant(2, 5, 6, "comparison", ">=", "Killed"),
            _mutant(2, 17, 18, "comparison", "<=", "Killed"),
            _mutant(4, 11, 12, "arithmetic", "-", "Timeout"),
        ],
    }
    return report


#: One mutant in each of the three states that are neither survived nor caught. They exist in this
#: suite only because the legend has to name them accurately and separately — the page used to call
#: all three "never run", which is the opposite of the truth for an errored one.
def _unscored_report() -> dict[str, Any]:
    return _report(
        [
            _mutant(2, 5, 6, "comparison", ">=", "Ignored"),
            _mutant(2, 11, 14, "boolean", "or", "CompileError"),
            _mutant(3, 10, 11, "numeric", "1", "RuntimeError"),
            _mutant(4, 11, 12, "arithmetic", "-", "Killed"),
        ]
    )


#: The findings' real addresses, taken from the view rather than written out here. The columns in
#: `_MUTANTS` are *source* columns and the page's are tab-expanded, so any hand-written key would
#: be quietly wrong — and a test that hard-codes what the code computes stops testing it.
KEYS = [
    finding_key(f.path, x.fid) for f in report_view(_report(_MUTANTS)).files for x in f.findings
]


@pytest.fixture(scope="module")
def observed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """What the page displayed while the harness drove it."""
    out = tmp_path_factory.mktemp("page")
    page = out / "report.html"
    multi, unscored = out / "multi.html", out / "unscored.html"
    page.write_text(render_html(_report(_MUTANTS)), encoding="utf-8")
    multi.write_text(render_html(_multi_report()), encoding="utf-8")
    unscored.write_text(render_html(_unscored_report()), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(page), str(multi), str(unscored)],
        capture_output=True,
        text=True,
        # Explicit, because the harness prints UTF-8 and Windows would otherwise decode its output
        # as cp1252 — the same legacy-code-page trap that has bitten this CLI's console output.
        encoding="utf-8",
        check=True,
        env={
            "HARNESS_OPS": json.dumps(_OPS),
            "HARNESS_KEYS": json.dumps(KEYS),
            "PATH": os.environ["PATH"],
            # A replaced env on Windows still needs SystemRoot -- without it, Windows' own
            # process loader (and Node's own init) can fail before the script even runs,
            # decades-old and unrelated to anything GDScript/Node-specific. Absent on other
            # platforms, so this is a no-op there.
            **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {}),
        },
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def test_the_page_opens_on_the_first_finding_rather_than_an_empty_panel(
    observed: dict[str, Any],
) -> None:
    assert observed["load"] == "1 of 4 findings"


def test_stepping_forward_walks_every_finding_and_stops_at_the_last(
    observed: dict[str, Any],
) -> None:
    # The regression this pins: comparing findings by a field they do not have made
    # `undefined === undefined` match element 0, so the index recomputed as 0 on every press and
    # navigation froze after a single move — with the button still enabled and the counter healthy.
    walked = observed["forward"]
    assert walked[:4] == [f"{n} of 4 findings" for n in range(1, 5)]
    assert observed["forwardEnd"] == {"pos": "4 of 4 findings", "nextDisabled": True}


def test_stepping_backward_walks_every_finding_and_stops_at_the_first(
    observed: dict[str, Any],
) -> None:
    assert observed["backward"][:4] == [f"{n} of 4 findings" for n in range(4, 0, -1)]
    assert observed["backwardEnd"] == {"pos": "1 of 4 findings", "prevDisabled": True}


def test_the_stepper_walks_exactly_what_the_filter_shows(observed: dict[str, Any]) -> None:
    # The stepper used to walk survivors regardless of the filter, so under "all" it reported a
    # live selection as "– of 4" — a position the reader could not reconcile with the marks on
    # screen. Each filter's count must be that filter's own set: 6 findings under "all", the 4 with
    # a surviving angle under "survived", the 2 every test caught under "caught".
    assert observed["filters"]["all"].endswith("of 6 findings")
    assert observed["filters"]["survived"].endswith("of 4 findings")
    assert observed["filters"]["caught"].endswith("of 2 findings")
    # …and in every one of them, stepping lands on a real position rather than a dash.
    for key, shown in observed["filters"].items():
        if key.endswith(":after-step"):
            assert not shown.startswith("–"), (key, shown)


def test_a_selection_survives_a_widening_filter_change(observed: dict[str, Any]) -> None:
    # Switching to "all" for context must not throw away the finding being read.
    assert observed["kept"]["under_survived"].endswith("of 4 findings")
    assert observed["kept"]["under_all"].endswith("of 6 findings")
    assert not observed["kept"]["under_all"].startswith("–")


def test_findings_are_reachable_from_the_keyboard_alone(observed: dict[str, Any]) -> None:
    # Every position the harness recorded came from an ArrowRight/ArrowLeft keypress, so the fact
    # that the walk happened at all is the assertion: with the marks click-only and the arrows
    # broken, there was no keyboard path to any finding.
    assert observed["forward"][0] != observed["forward"][4]


# ---- deep links --------------------------------------------------------------------------------


def test_the_selected_finding_is_addressable_so_a_reload_keeps_your_place(
    observed: dict[str, Any],
) -> None:
    # All page state used to live in JS variables, so a refresh threw your place away and "look at
    # this survivor" was not something you could paste anywhere. The address is the stable key.
    # Addressed by KEYS, never by hand-written columns: the page counts columns tab-expanded and
    # `_MUTANTS` gives them in source columns, so every literal key here was quietly wrong.
    deep = observed["deep"]
    assert deep["onLoad"] == f"#{KEYS[0]}"
    assert deep["link"] == f"#{KEYS[3]}"
    assert deep["restoredPos"] == deep["posAtLink"] == "3 of 4 findings"
    assert deep["restoredHash"] == deep["link"]


def test_a_link_to_a_caught_finding_widens_the_filter_so_it_is_actually_on_screen(
    observed: dict[str, Any],
) -> None:
    # The default filter shows survivors only. Resolving the link correctly and then landing on a
    # pane that does not contain it reads exactly like a broken link.
    assert observed["deep"]["caughtPos"] == "6 of 6 findings"
    assert observed["deep"]["caughtHash"] == f"#{KEYS[5]}"


def test_a_link_whose_finding_has_moved_falls_back_instead_of_landing_somewhere_wrong(
    observed: dict[str, Any],
) -> None:
    # An id that does not match exactly does not match at all — the danger is not a dead link, it
    # is a link that quietly resolves to the wrong survivor. The file it named is still the best
    # answer, so the page opens there on its first finding.
    assert observed["deep"]["movedPos"] == "1 of 4 findings"
    assert observed["deep"]["movedHash"] == f"#{KEYS[0]}"


def test_a_hash_that_resolves_to_nothing_opens_the_default_view_rather_than_erroring(
    observed: dict[str, Any],
) -> None:
    # A file this run did not cover, and outright garbage — including a percent-escape that throws
    # inside decodeURIComponent, which is a real crash and not a hypothetical one.
    assert observed["deep"]["gonePos"] == "1 of 4 findings"
    assert observed["deep"]["junkPos"] == "1 of 4 findings"


def test_pasting_a_link_into_an_already_open_report_navigates_it(
    observed: dict[str, Any],
) -> None:
    # Same document, so nothing reloads on its own; without a hashchange handler the URL would
    # change and the page would sit there.
    assert observed["deep"]["beforePaste"] == "3 of 4 findings"
    assert observed["deep"]["afterPaste"] == "1 of 4 findings"


# ---- the click audit ---------------------------------------------------------------------------
#
# The bug these exist for: `#prev` / `#next` shipped drawn, labelled, with `paintStepper` faithfully
# maintaining their disabled state — and wired to nothing. They were decorative. Every test above
# passed anyway, because the harness pressed keys and called `step()` instead of clicking the
# elements. So each control is now clicked the way a browser clicks it — through the page's own
# delegated handler, on an element the page's `closest()` lookup really matches — and the assertion
# is that the page moved.


def test_the_stepper_arrows_move_the_selection_when_they_are_clicked(
    observed: dict[str, Any],
) -> None:
    clicks = observed["clicks"]
    assert clicks["start"] == "1 of 4 findings"
    assert clicks["next"] == "2 of 4 findings"
    assert clicks["nextAgain"] == "3 of 4 findings"
    assert clicks["prev"] == "2 of 4 findings"


def test_the_filter_and_mutator_chips_respond_to_a_click(observed: dict[str, Any]) -> None:
    assert observed["clicks"]["filterAll"].endswith("of 6 findings")
    assert observed["clicks"]["opComparison"].endswith("of 2 findings")


def test_clicking_a_mark_in_the_source_selects_its_finding(observed: dict[str, Any]) -> None:
    # The marks were the one control that *was* wired before, via a handler on `#src` that the
    # rearchitecture moved to `#body`. Clicking one must still land on that finding.
    assert observed["clicks"]["mark"] == "3 of 4 findings"


def test_the_reference_disclosure_responds_to_a_click(observed: dict[str, Any]) -> None:
    assert observed["clicks"]["refBefore"] is False
    assert observed["clicks"]["refAfter"] is True


def test_the_theme_toggle_responds_to_a_click(observed: dict[str, Any]) -> None:
    # It lives in the masthead, outside `#body`, so it is the one control that keeps its own
    # handler — which makes it exactly the one a delegation rewrite could strand.
    assert observed["clicks"]["themeBefore"] != observed["clicks"]["themeAfter"]
    assert observed["clicks"]["themeAfter"] == "dark"


def test_frank_winks_on_hover_and_reverts(observed: dict[str, Any]) -> None:
    # He lives in the masthead too, and keeps his own handler for the same reason the theme toggle
    # does — except his trigger is a hover, not a click (see hoverFrank in the harness, which never
    # goes through the delegated #body click path at all). The wink is timed (a setTimeout revert),
    # so this also proves the revert actually fires rather than leaving him stuck mid-face — a
    # markup-only check could not tell that apart from a class that never gets removed.
    clicks = observed["clicks"]
    assert clicks["frankBefore"] is False
    assert clicks["frankDuring"] is True
    assert clicks["frankAfter"] is False


def test_frank_also_winks_entirely_on_his_own(observed: dict[str, Any]) -> None:
    # A fresh tab, never hovered or focused at all: proves the auto-wink schedule really does
    # trigger him unprompted, not only in response to a reader's own hover or focus.
    auto = observed["frankAuto"]
    assert auto["before"] is False
    assert auto["afterFirstDrain"] is True


def test_frank_also_winks_on_keyboard_focus(observed: dict[str, Any]) -> None:
    # A reader who never touches a mouse still tabs to every other control in the masthead, so
    # focusing Frank has to trigger the same reaction hovering him does, not a mouse-only Easter
    # egg a keyboard user cannot reach at all.
    clicks = observed["clicks"]
    assert clicks["frankFocusBefore"] is False
    assert clicks["frankFocusDuring"] is True
    assert clicks["frankFocusAfter"] is False


def test_an_index_row_opens_its_file_and_the_back_button_returns(observed: dict[str, Any]) -> None:
    index = observed["index"]
    # A two-file run opens on the index, which has no address of its own.
    assert index["openIndex"] is True
    assert index["openHash"] == ""
    # Clicking the second row opens that file, on its own first finding, at its own address.
    assert index["afterRow"] == {
        "pos": "1 of 1 finding",
        "hash": f"#b.gd:{KEYS[0].split(':', 1)[1]}",
        "index": False,
    }
    assert index["afterBack"] == {"hash": "", "index": True}


def test_escape_returns_to_the_index_exactly_as_the_back_button_does(
    observed: dict[str, Any],
) -> None:
    assert observed["index"]["afterEscape"]["reachedFile"].startswith("#a.gd:")
    assert observed["index"]["afterEscape"]["hash"] == ""


# ---- the legend --------------------------------------------------------------------------------


def test_the_legend_explains_only_the_marks_the_pane_actually_drew(
    observed: dict[str, Any],
) -> None:
    # It used to be fixed markup listing the whole palette, so on the corpus fixture — where
    # ignored, invalid and errored are all zero — a reader was taught a grey they would never see
    # and had no way to recognise. It is now built from the marks on screen, and narrows with the
    # filter for the same reason.
    legend = observed["legend"]
    assert "survived" in legend["survived"] and "caught by a test" not in legend["survived"]
    assert "caught by a test" in legend["caught"] and "no test caught it" not in legend["caught"]
    assert "no test caught it" in legend["all"] and "caught by a test" in legend["all"]
    # None of the three unscored states occurs in this report, so none of them is explained.
    for absent in ("never ran", "errored", "did not parse"):
        assert absent not in legend["all"], absent


def test_the_legend_names_each_unscored_state_and_never_calls_an_errored_mutant_never_run(
    observed: dict[str, Any],
) -> None:
    # `Verdict` in `gdmutant/engine/loop.py` is where these are decided, and it is unambiguous:
    # ignored and invalid are never run, but an errored mutant is one the runner FAILED WHILE
    # RUNNING. Grouping all three under "never run (ignored, invalid or errored)" stated the
    # opposite of what happened to the third.
    unscored = observed["legend"]["unscored"]
    assert "ignored: a <code># gdmutant: ignore</code> annotation, so it never ran" in unscored
    assert "invalid: the mutation did not parse, so it never ran" in unscored
    assert "errored: the runner failed while running it" in unscored
    assert "never run" not in unscored


def test_a_multi_finding_mark_says_how_many_and_what_a_click_does(
    observed: dict[str, Any],
) -> None:
    # "click to cycle" was vague, and on the common case — exactly two — "cycle" is the wrong word
    # for what the click does. The count comes from the group, so it is the real number.
    assert observed["legend"]["multiMark"] == "2 findings here, click to switch between them"
    assert observed["legend"]["multiBadge"] == "2"
    assert "the badge is how many" in observed["legend"]["all"]


# ---- the header's rare-status counts ------------------------------------------------------------


def test_a_rare_status_count_in_the_header_reaches_the_mutants_behind_it(
    observed: dict[str, Any],
) -> None:
    # On a real run these read 60 timeout, 8 compile errors and 204 runtime errors, with no way to
    # get to any of them. The counts live outside `#body`, so this click travels a wiring nothing
    # else on the page uses, which is exactly the wiring that could ship drawn and unconnected.
    rare = observed["rare"]
    assert rare["start"] == "no findings"  # this report has no survivors, and survivors is default
    for status in ("Ignored", "CompileError", "RuntimeError"):
        assert rare[status] == "1 of 1 finding", status
        assert rare[f"{status}:after-step"] == "1 of 1 finding", status


def test_the_three_rare_states_stay_three_things_and_do_not_collapse_into_one(
    observed: dict[str, Any],
) -> None:
    # The distinction is the whole value of surfacing them. A runtime error is the actionable one:
    # the mutant was valid, it ran, and the harness fell over, so it measured nothing, and a big
    # count there is a blind spot in the score rather than a curiosity.
    card = observed["rare"]["runtimeCard"]
    assert "the run errored" in card
    assert "did not parse" not in card and "never ran" not in card


def test_a_rare_count_is_not_narrowed_by_an_operator_chip_left_from_an_earlier_click(
    observed: dict[str, Any],
) -> None:
    # `matches()` ANDs the status filter with the operator chip. A header count is a claim about the
    # whole report, so a chip still narrowed to some other operator could hide exactly the mutants
    # the count promised: the header says "1 runtime error", the click lands on "no findings", and
    # nothing on screen says an unrelated filter is why. Every other assertion in this section
    # clicks a count with the operator left at its default, so none of them can see this.
    stale = observed["staleOp"]
    assert stale["afterOpChip"] == "no findings"  # `comparison` holds an Ignored, not a survivor
    assert stale["afterHeaderCount"] == "1 of 1 finding"
    # And it is the counted mutant that got opened, not merely some finding.
    assert "numeric" in stale["card"]
    assert "the run errored" in stale["card"]


def test_a_rare_count_clicked_from_the_index_opens_a_file_that_actually_has_one(
    observed: dict[str, Any],
) -> None:
    # A header count is a claim about the whole report, so it can be clicked with no source pane on
    # screen at all. The only file holding a timeout is not the one the index opens on, so landing
    # on a finding is proof the click went somewhere rather than merely setting a variable.
    assert observed["rareIndex"]["openedOnIndex"] is True
    assert observed["rareIndex"]["afterClick"] == {"index": False, "pos": "1 of 1 finding"}


# ---- the file index's sortable columns ----------------------------------------------------------


def test_the_index_still_opens_on_most_survivors_first(observed: dict[str, Any]) -> None:
    # The default is the one order that answers "where do I start", and re-sorting must not become
    # a reason to change it. Score would be a worse default: 1 survivor in 5 mutants and 100 in 500
    # both read 80%.
    assert observed["sort"]["initial"] == ["a.gd", "b.gd", "c.gd"]


def test_clicking_a_column_re_sorts_and_clicking_it_again_reverses(
    observed: dict[str, Any],
) -> None:
    sort = observed["sort"]
    assert sort["survivedAsc"] == ["c.gd", "b.gd", "a.gd"]
    assert sort["survivedBack"] == sort["initial"]
    assert sort["file"] == ["a.gd", "b.gd", "c.gd"]
    assert sort["fileDesc"] == ["c.gd", "b.gd", "a.gd"]


def test_each_column_sorts_on_its_own_number_rather_than_re_drawing_the_same_order(
    observed: dict[str, Any],
) -> None:
    # The fixture is built so no two of these agree; a sort that quietly did nothing would show up
    # as one of them matching the default.
    sort = observed["sort"]
    assert sort["mutants"] == ["a.gd", "c.gd", "b.gd"]
    assert sort["caught"] == ["c.gd", "a.gd", "b.gd"]
    # Score's order is the argument against making it the default, written out: the file with
    # nothing surviving sorts to the top and the file with five survivors sits below it.
    assert sort["score"] == ["c.gd", "a.gd", "b.gd"]
    assert sort["score"] != sort["initial"]


def test_a_re_sorted_row_still_opens_the_file_it_names(observed: dict[str, Any]) -> None:
    # The row carries the file's index into the report, not its position on screen. A sort that
    # renumbered them would open the wrong file, silently, and only after someone re-sorted.
    sort = observed["sort"]
    assert sort["fileIds"] == ["0", "1", "2"]
    assert sort["fileDescIds"] == ["2", "1", "0"]
    assert sort["openedFirstDrawn"] == "#c.gd"


# ---- the JSON download --------------------------------------------------------------------------


def test_the_download_button_hands_back_the_report_the_page_is_carrying(
    observed: dict[str, Any],
) -> None:
    # Clicked for real, and the bytes are parsed rather than string-matched, so a button wired to
    # the wrong element would fail here instead of passing on a plausible-looking blob.
    download = observed["download"]
    assert download["before"] == 0
    assert download["count"] == 1
    assert download["name"] == "gdmutant-report.json"
    assert download["type"] == "application/json"
    assert download["parsed"] == _report(_MUTANTS)
    # The object URL is released after the click; a reader may download more than once.
    assert download["revoked"] is True


# ---- the browser's own back button ---------------------------------------------------------------


def test_opening_a_file_from_the_index_gives_the_browser_something_to_go_back_to(
    observed: dict[str, Any],
) -> None:
    # Before this, the browser's back button left the report entirely and landed on whatever page
    # preceded it. Every move replaced, so the report had put nothing in the history at all.
    history = observed["history"]
    assert history["onOpen"] == {"depth": 1, "cursor": 0, "index": True}
    assert history["afterOpen"] == {"depth": 2, "cursor": 1, "index": False}


def test_stepping_between_findings_still_leaves_the_history_alone(
    observed: dict[str, Any],
) -> None:
    # The reason `replaceState` was chosen, and it has to survive the change: 197 findings must not
    # become 197 back-presses between the reader and wherever they came from. Three moves here,
    # two keypresses and a real click on the arrow, and the history is exactly as it was.
    after = observed["history"]["afterSteps"]
    assert after == {"depth": 2, "cursor": 1, "pos": "4 of 4 findings"}


def test_the_browsers_back_button_returns_to_the_index_like_the_pages_own_does(
    observed: dict[str, Any],
) -> None:
    history = observed["history"]
    assert history["afterBack"] == {"depth": 2, "cursor": 0, "index": True}
    # The in-page button pushes the same kind of entry, so the two controls agree instead of one
    # of them doing something unrelated.
    assert history["afterInPageBack"] == {"depth": 3, "cursor": 2, "index": True}


def test_a_single_file_report_puts_nothing_in_the_history_at_all(
    observed: dict[str, Any],
) -> None:
    # It has no index, so it has no structural move to record, and pushing an entry for one would
    # create a state the page cannot render. Six keypresses, an arrow click and a mark click later,
    # the history is untouched.
    solo = observed["history"]["solo"]
    assert solo["onOpen"] == 1
    assert solo["afterSteps"] == {"depth": 1, "cursor": 0, "pos": "3 of 4 findings"}


def test_a_deep_link_still_opens_on_its_finding_and_costs_no_entry(
    observed: dict[str, Any],
) -> None:
    assert observed["history"]["deepLink"] == {"depth": 1, "pos": "3 of 4 findings"}


def test_every_mark_is_a_real_button_so_tab_reaches_it() -> None:
    # Marks were click-only <span>s with no tabindex or role. A <button> is focusable and
    # activates on Enter/Space for free.
    page = render_html(
        {
            "schemaVersion": "2",
            "files": {"a.gd": {"language": "gdscript", "source": _SOURCE, "mutants": _MUTANTS}},
        }
    )
    assert 'class="mark ' not in page.replace('<button type="button" class="mark ', "")
    assert '<button type="button" class="mark ' in page.split("function paintSource", 1)[1]
