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

from gdmutant.engine.htmlreport import render_html

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


@pytest.fixture(scope="module")
def observed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """What the page displayed while the harness drove it."""
    report = {
        "schemaVersion": "2",
        "files": {"a.gd": {"language": "gdscript", "source": _SOURCE, "mutants": _MUTANTS}},
    }
    page = tmp_path_factory.mktemp("page") / "report.html"
    page.write_text(render_html(report), encoding="utf-8")
    result = subprocess.run(
        ["node", str(HARNESS), str(page)],
        capture_output=True,
        text=True,
        check=True,
        env={"HARNESS_OPS": json.dumps(_OPS), "PATH": __import__("os").environ["PATH"]},
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
