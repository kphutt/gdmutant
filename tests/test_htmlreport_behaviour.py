"""Run the report page's real inlined script, and assert on what it does.

The page's triage loop is behaviour, not markup: stepping through findings, changing filters,
reaching a finding from the keyboard. Asserting on the generated HTML would only re-state the
template. So `tests/js/harness.js` executes the shipped script against a recording DOM stand-in and
prints what the page displayed; this module drives that and checks it.

Skipped when `node` is not on PATH — the rest of the suite (and the whole CLI) stays Node-free.
"""

import json
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

#: The same run again after a test was added that kills the first survivor. Same source, so every
#: finding keeps its id — but the file's stamp moves, which is what lets the page tell a mark made
#: against *this* run from one carried over from the run before it.
_RERUN = [
    {**m, "status": "Killed"} if m["location"]["start"]["column"] == 5 else m for m in _MUTANTS
]

_OPS = ["arithmetic", "boolean", "comparison", "numeric", "statement-deletion"]


def _report(mutants: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "2",
        "files": {"a.gd": {"language": "gdscript", "source": _SOURCE, "mutants": mutants}},
    }


#: A second file, so the run opens on the file index — the only place the index rows and the
#: "all files" button exist to be clicked at all.
def _multi_report() -> dict[str, Any]:
    report = _report(_MUTANTS)
    report["files"]["b.gd"] = {
        "language": "gdscript",
        "source": _SOURCE,
        "mutants": [_mutant(2, 5, 6, "comparison", ">=", "Survived")],
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
    page, rerun = out / "report.html", out / "rerun.html"
    multi, unscored = out / "multi.html", out / "unscored.html"
    page.write_text(render_html(_report(_MUTANTS)), encoding="utf-8")
    rerun.write_text(render_html(_report(_RERUN)), encoding="utf-8")
    multi.write_text(render_html(_multi_report()), encoding="utf-8")
    unscored.write_text(render_html(_unscored_report()), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(page), str(rerun), str(multi), str(unscored)],
        capture_output=True,
        text=True,
        # Explicit, because the harness prints UTF-8 and Windows would otherwise decode its output
        # as cp1252 — the same legacy-code-page trap that has bitten this CLI's console output.
        encoding="utf-8",
        check=True,
        env={
            "HARNESS_OPS": json.dumps(_OPS),
            "HARNESS_KEYS": json.dumps(KEYS),
            "PATH": __import__("os").environ["PATH"],
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


# ---- done marks --------------------------------------------------------------------------------


def test_done_marks_count_what_is_on_screen_and_toggle_both_ways(
    observed: dict[str, Any],
) -> None:
    marks = observed["marks"]
    assert marks["start"] == "0 of 4 done"
    assert marks["afterOne"] == "1 of 4 done"
    assert marks["afterTwo"] == "2 of 4 done"
    assert marks["afterUnmark"] == "1 of 4 done"
    assert marks["beforeReload"] == "2 of 4 done"


def test_done_marks_survive_a_reload_and_land_back_on_the_same_findings(
    observed: dict[str, Any],
) -> None:
    # Persisting a count would be easy and useless. What has to survive is *which* findings — so
    # the assertion walks the list and reads each card's own control.
    assert observed["marks"]["afterReload"] == "2 of 4 done"
    assert observed["marks"]["states"] == ["done", "", "done", ""]


def test_a_copy_that_travelled_opens_unmarked_rather_than_inheriting_progress(
    observed: dict[str, Any],
) -> None:
    # Same browser storage, different report location. Losing marks is the safe failure here;
    # showing a stranger's — or your own, from another project — is not.
    assert observed["marks"]["elsewhere"] == "0 of 4 done"


def test_storage_that_refuses_costs_the_marks_and_nothing_else(
    observed: dict[str, Any],
) -> None:
    # Private windows, a full quota, storage switched off. A report someone opened to read must
    # not break over a feature they are not using.
    assert observed["marks"]["brokenPos"] == "1 of 4 findings"
    assert observed["marks"]["brokenDone"] == "1 of 4 done"  # works, just does not persist


def test_a_mark_carried_over_from_an_earlier_run_is_flagged_not_counted_as_done(
    observed: dict[str, Any],
) -> None:
    # THE failure this feature could introduce: a stale tick hiding a live survivor — the exact
    # miss gdmutant exists to prevent. After a re-run, a mark on a finding that is *still
    # surviving* reads "re-check", is styled apart, and contributes nothing to the count. The
    # marked finding that the re-run actually killed simply drops out of the survivor list.
    assert observed["stale"]["done"] == "0 of 3 done · 1 to re-check"
    assert observed["stale"]["states"] == ["", "recheck", ""]


def test_acknowledging_a_re_check_is_one_keypress_and_sticks(observed: dict[str, Any]) -> None:
    assert observed["stale"]["ackFound"] == "recheck"
    assert observed["stale"]["afterAck"] == "1 of 3 done"
    assert observed["stale"]["afterAckReload"] == "1 of 3 done"


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


def test_the_done_control_and_the_reference_disclosure_respond_to_a_click(
    observed: dict[str, Any],
) -> None:
    assert observed["clicks"]["doneBefore"] == "0 of 4 done"
    assert observed["clicks"]["doneAfter"] == "1 of 4 done"
    assert observed["clicks"]["refBefore"] is False
    assert observed["clicks"]["refAfter"] is True


def test_the_theme_toggle_responds_to_a_click(observed: dict[str, Any]) -> None:
    # It lives in the masthead, outside `#body`, so it is the one control that keeps its own
    # handler — which makes it exactly the one a delegation rewrite could strand.
    assert observed["clicks"]["themeBefore"] != observed["clicks"]["themeAfter"]
    assert observed["clicks"]["themeAfter"] == "dark"


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
    assert "ignored — a <code># gdmutant: ignore</code> annotation, so it never ran" in unscored
    assert "invalid — the mutation did not parse, so it never ran" in unscored
    assert "errored — the runner failed while running it" in unscored
    assert "never run" not in unscored


def test_a_multi_finding_mark_says_how_many_and_what_a_click_does(
    observed: dict[str, Any],
) -> None:
    # "click to cycle" was vague, and on the common case — exactly two — "cycle" is the wrong word
    # for what the click does. The count comes from the group, so it is the real number.
    assert observed["legend"]["multiMark"] == "2 findings here, click to switch between them"
    assert observed["legend"]["multiBadge"] == "2"
    assert "the badge is how many" in observed["legend"]["all"]


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
