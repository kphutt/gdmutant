"""Live check of marker placement (docs/decisions/0017, Plan step 1) against a REAL Godot.

The ADR's bar for step 1: every marked corpus file parses, keeps its error line numbers, and every
spot that can take a marker fires. Unit tests cannot show any of that, because only Godot decides
what it accepts and which code runs. So this file marks the corpus in a throwaway copy and runs it.

Env-gated on ``GDMUTANT_GODOT`` like tests/test_selftest_live.py, so a plain ``uv run pytest``
skips it. The GdUnit4 and GUT halves also need their addons (``scripts/install_gdunit4.py`` and
``scripts/install_gut.py``) and skip without them.

Nothing records hits in gdmutant yet: that is step 2. So this file brings a stand-in recorder.
`_GdmMarks` is a ``class_name`` script with a static ``hit``, which the marker text
``_GdmMarks.hit(N); `` calls exactly as it would call an autoload of that name. It is a class and
not the autoload the ADR describes because the corpus's command harness loads the code under test
in its ``_init``, before Godot has registered any autoload. A marker naming an autoload fails to
compile there ("Identifier not found"), while a global class resolves. A small autoload writes the
hits to a file when Godot exits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import generate_mutants
from gdmutant.adapters.gdscript.markers import MarkedSource, RunEverything, place_markers
from gdmutant.adapters.gdscript.runner import GdUnit4Runner, GutRunner
from gdmutant.engine.runner import CommandRunner, Runner, SuiteResult

_GODOT = os.environ.get("GDMUTANT_GODOT")

pytestmark = pytest.mark.skipif(
    not _GODOT, reason="set GDMUTANT_GODOT=<godot path> to run the live marker check"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "corpus"
HITS = "_gdm_hits.json"

_RECORDER = """class_name _GdmMarks
extends Object
## Stand-in marker recorder for the live test. Records each spot once.
static var hits := {}
static func hit(spot: int) -> void:
\thits[spot] = true
"""

_WRITER = f"""extends Node
## Writes the recorded spots when Godot exits, the way the ADR collects them.
func _notification(what: int) -> void:
\tif what == NOTIFICATION_PREDELETE:
\t\tvar out := FileAccess.open("res://{HITS}", FileAccess.WRITE)
\t\tout.store_string(JSON.stringify(_GdmMarks.hits.keys()))
"""

# A driver that calls every function in turn_order.gd, so every spot in it can fire. `_initialize`
# rather than `_init`, so it runs after the autoload that writes the hits exists.
_CALL_EVERYTHING = """extends SceneTree
func _initialize() -> void:
\tvar t: GDScript = load("res://turn_order.gd")
\tt.acts_before(5, 3)
\tt.clamp_initiative(-3, 10)
\tt.clamp_initiative(12, 10)
\tt.clamp_initiative(6, 10)
\tt.is_adjacent(1, 1, 1, 2)
\tt.can_act(true, false)
\tt.ties_favor_earlier()
\tquit()
"""

# The elif rule's proof case: `n > 50` -> `n >= 50` is killed by grade(50), whose path evaluates
# the elif condition and never enters the elif body.
_GRADE = """extends RefCounted
static func grade(n: int) -> String:
\tif n > 90:
\t\treturn "A"
\telif n > 50:
\t\treturn str(n - 1)
\treturn "C"
"""
_CALL_GRADE_50 = """extends SceneTree
func _initialize() -> void:
\tvar g: GDScript = load("res://grade.gd")
\tprint("GRADE|", g.grade(50))
\tquit()
"""

# Line 7 fails at runtime, and line 4 of `bad.gd` fails to compile. Both lines hold a mutant, so
# both get a marker in front of them, and the error must still name the same line.
_RUNTIME_ERROR = """extends RefCounted
static func boom(n: int) -> int:
\tvar total := n + 1
\tif total > 1:
\t\tvar missing: Variant = null
\t\tprint(total - 1)
\t\treturn missing.size() + total
\treturn total
"""
_COMPILE_ERROR = """extends RefCounted
static func f(a: int) -> int:
\tvar b := a + 1
\tvar s: String = b - 1
\treturn b
"""
_CALL_BOOM = """extends SceneTree
func _initialize() -> void:
\tvar b: GDScript = load("res://boom.gd")
\tb.boom(5)
\tload("res://bad.gd")
\tquit()
"""


def _project(tmp_path: Path, name: str, extra: dict[str, str], marked: bool) -> Path:
    """A copy of the corpus with `extra` files added, the recorder installed, and, if `marked`,
    every .gd file that has mutants replaced by its marked source. The unmarked copy gets the same
    recorder so the markers are the only difference between the two."""
    project = tmp_path / name
    shutil.copytree(CORPUS, project, ignore=shutil.ignore_patterns(".godot", "reports"))
    for relative, text in extra.items():
        (project / relative).write_text(text, encoding="utf-8", newline="\n")
    (project / "_gdm_marks.gd").write_text(_RECORDER, encoding="utf-8", newline="\n")
    (project / "_gdm_writer.gd").write_text(_WRITER, encoding="utf-8", newline="\n")
    settings = project / "project.godot"
    settings.write_text(
        settings.read_text(encoding="utf-8")
        + '\n[autoload]\n\n_GdmHitsWriter="*res://_gdm_writer.gd"\n',
        encoding="utf-8",
        newline="\n",
    )
    if marked:
        for target in ["turn_order.gd", *extra]:
            path = project / target
            source = path.read_text(encoding="utf-8")
            result = place_markers(source, generate_mutants(target, source))
            path.write_text(result.source, encoding="utf-8", newline="\n")
    assert _GODOT is not None
    subprocess.run(
        [_GODOT, "--headless", "--path", str(project), "--import"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert (project / ".godot").is_dir(), "Godot --import did not create the .godot cache"
    return project


def _marked(relative: str) -> MarkedSource:
    source = (CORPUS / relative).read_text(encoding="utf-8")
    return place_markers(source, generate_mutants(relative, source))


def _godot(project: Path, script: str) -> str:
    """Run a SceneTree script in `project` and return its combined output."""
    assert _GODOT is not None
    done = subprocess.run(
        [_GODOT, "--headless", "--path", str(project), "--script", script],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return done.stdout + done.stderr


def _hits(project: Path) -> set[int]:
    """The spots the last run recorded. The file is written at exit even when nothing was hit,
    so a missing file means the writer never ran, never that no spot fired."""
    path = project / HITS
    assert path.is_file(), f"no {HITS}: the hit writer did not run"
    hits = {int(spot) for spot in json.loads(path.read_text(encoding="utf-8"))}
    path.unlink()
    return hits


def _runners() -> list[tuple[str, Runner]]:
    assert _GODOT is not None
    harness = [_GODOT, "--headless", "--path", ".", "--script", "res://harness/run_tests.gd"]
    runners: list[tuple[str, Runner]] = [("command", CommandRunner(command=harness))]
    if (CORPUS / "addons" / "gdUnit4").is_dir():
        runners.append(("gdunit4", GdUnit4Runner(godot=_GODOT)))
    if (CORPUS / "addons" / "gut").is_dir():
        runners.append(("gut", GutRunner(test_dir="res://gut_test", godot=_GODOT)))
    return runners


@pytest.mark.parametrize("runner_name", ["command", "gdunit4", "gut"])
def test_the_marked_corpus_passes_its_tests_exactly_as_before(
    tmp_path: Path, runner_name: str
) -> None:
    runners = dict(_runners())
    if runner_name not in runners:
        pytest.skip(f"{runner_name} addon not installed")
    runner = runners[runner_name]
    results: dict[bool, SuiteResult] = {}
    hits: dict[bool, set[int]] = {}
    for marked in (False, True):
        project = _project(tmp_path, f"{runner_name}-{marked}", {}, marked)
        results[marked] = runner.run(str(project))
        hits[marked] = _hits(project)
    plain, marked_result = results[False], results[True]
    assert plain.passed, plain.detail
    assert (marked_result.tests, marked_result.failures, marked_result.errors) == (
        plain.tests,
        plain.failures,
        plain.errors,
    ), marked_result.detail
    assert plain.tests > 0
    assert hits[False] == set()  # no markers, no hits: the writer reports only what fired
    # The corpus tests leave can_act and ties_favor_earlier untested on purpose. Their spots must
    # stay silent, and every other spot must fire.
    spots = {spot.line: spot.id for spot in _marked("turn_order.gd").spots}
    untested = {spots[27], spots[32]}
    assert hits[True] == set(spots.values()) - untested


def test_every_spot_fires_when_every_function_is_called(tmp_path: Path) -> None:
    project = _project(tmp_path, "all", {"_call.gd": _CALL_EVERYTHING}, marked=True)
    output = _godot(project, "res://_call.gd")
    assert "SCRIPT ERROR" not in output, output
    result = _marked("turn_order.gd")
    assert not any(isinstance(p, RunEverything) for p in result.placements)
    assert _hits(project) == {spot.id for spot in result.spots}


def test_an_elif_condition_marker_fires_on_a_path_that_skips_the_elif_body(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path, "elif", {"grade.gd": _GRADE, "_call.gd": _CALL_GRADE_50}, marked=True
    )
    output = _godot(project, "res://_call.gd")
    assert "GRADE|C" in output, output
    mutants = generate_mutants("grade.gd", _GRADE)
    result = place_markers(_GRADE, mutants)
    (elif_spot,) = {
        placement
        for mutant, placement in zip(mutants, result.placements, strict=True)
        if mutant.span.line == 5 and mutant.original == ">"
    }
    body_spot = next(spot.id for spot in result.spots if spot.line == 6)
    hits = _hits(project)
    assert elif_spot in hits
    assert body_spot not in hits  # the path really did skip the elif body


def test_marked_code_reports_errors_on_the_same_lines(tmp_path: Path) -> None:
    files = {"boom.gd": _RUNTIME_ERROR, "bad.gd": _COMPILE_ERROR, "_call.gd": _CALL_BOOM}
    lines: dict[bool, list[tuple[str, str]]] = {}
    for marked in (False, True):
        project = _project(tmp_path, f"errors-{marked}", files, marked)
        output = _godot(project, "res://_call.gd")
        lines[marked] = re.findall(r"res://(boom|bad)\.gd:(\d+)", output)
    # Both errors really happened, on the lines the fixtures put them on.
    assert ("boom", "7") in lines[False]
    assert ("bad", "4") in lines[False]
    assert lines[True] == lines[False]
    # And the marked copy did put a marker on both of those lines.
    for name, text, line in (("boom.gd", _RUNTIME_ERROR, 7), ("bad.gd", _COMPILE_ERROR, 4)):
        spots = place_markers(text, generate_mutants(name, text)).spots
        assert line in {spot.line for spot in spots}
