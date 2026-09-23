"""Live self-test — drive the REAL gdmutant CLI against a REAL Godot binary + the corpus.

This is the end-to-end sanity check that closes the "never run against real Godot" gate:
both runner paths are exercised through the *shipped* CLI (argparse / exit codes / Stryker JSON —
via subprocess, never in-process) and pinned to *exact* per-mutant outcomes, not just "it ran".

It is **env-gated** on ``GDMUTANT_GODOT`` (the path to a godot executable), so a plain
``uv run pytest`` — local dev and the ``verify`` CI job — auto-skips it with zero config. Run it
with, e.g.::

    GDMUTANT_GODOT=godot uv run pytest tests/test_selftest_live.py -v

The CommandRunner test needs only Godot. The GdUnit4 test additionally skips if the addon is not
installed (run ``python scripts/install_gdunit4.py`` first).

The pinned expectations below are the *observed* result of running gdmutant against the corpus on a
real Godot — the three outcome classes the fixture is designed to show:
  * **killed** — a test catches the change (e.g. line 8 ``>`` -> ``>=``);
  * **coverage-gap survivors** — ``can_act`` / ``ties_favor_earlier`` are untested, and the clamp
    boundary at line 13 is never probed;
  * **equivalent survivors** — lines 13/15 ``<``/``>`` -> ``<=``/``>=`` yield the same value at the
    boundary, so they survive *despite* coverage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from gdmutant.adapters.gdscript import ADAPTER, generate_mutants
from gdmutant.adapters.gdscript.marker_run import RECORDER_DIR, WRITER_AUTOLOAD, GDScriptMarker
from gdmutant.adapters.gdscript.markers import MarkedSource, RunEverything, place_markers
from gdmutant.adapters.gdscript.runner import GdUnit4Runner, GutRunner
from gdmutant.engine.coverage import CoverageAnalysis, MarkedCopy
from gdmutant.engine.loop import (
    CoverageRunFailed,
    CoverageSelfCheckFailed,
    MutationRun,
    Verdict,
)
from gdmutant.engine.loop import run as engine_run
from gdmutant.engine.runner import CommandRunner, Runner, SuiteResult

_GODOT = os.environ.get("GDMUTANT_GODOT")

pytestmark = pytest.mark.skipif(
    not _GODOT, reason="set GDMUTANT_GODOT=<godot path> to run the live self-test"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "corpus"
ADDON = CORPUS / "addons" / "gdUnit4"
GUT_ADDON = CORPUS / "addons" / "gut"
TARGET = "turn_order.gd"

# The exact per-mutant outcome of running gdmutant against corpus/turn_order.gd on real Godot.
# Survivors pinned as (line, column, replacement) so a regression that flips one verdict, shifts a
# location, or drops a mutant is caught — a bare "score > 0" self-test would be worthless.
# 18 = 16 token mutants + 2 statement-deletions (the early `return 0`/`return max_value` inside
# clamp_initiative's ifs, both killed). The other 5 returns are typed sole-returns whose deletion
# Godot rejects, so the generation-time guard never emits them (docs/decisions/0007) — which is why
# there are 0 timeout/error outcomes and both runner paths still agree exactly.
EXPECTED_TOTAL = 18
EXPECTED_KILLED = 11
EXPECTED_SURVIVORS: set[tuple[int, int, str]] = {
    (13, 11, "<="),  # equivalent: value < 0  ->  value <= 0  (same clamp result)
    (13, 13, "1"),  # coverage gap: boundary at value 0 never probed
    (13, 13, "-1"),  # coverage gap
    (15, 11, ">="),  # equivalent: value > max  ->  value >= max
    (27, 15, "or"),  # coverage gap: can_act is untested
    (27, 19, ""),  # coverage gap: `not` deletion in can_act
    (32, 9, "false"),  # coverage gap: ties_favor_earlier is untested
}


def _corpus_copy(tmp_path: Path) -> Path:
    """Copy the corpus into a tmp dir and warm Godot's import cache; the repo copy is never touched.

    ``.godot`` / ``reports`` from earlier local runs are excluded so each run starts clean.
    """
    dst = tmp_path / "corpus"
    shutil.copytree(CORPUS, dst, ignore=shutil.ignore_patterns(".godot", "reports"))
    # Warm-up: GdUnit4 references TurnOrder by class_name, which only the import scan writes into
    # .godot/global_script_class_cache.cfg. Give it its own timeout and IGNORE its exit code
    # (--import returns non-zero on benign addon/import chatter across versions); assert the
    # artifact we actually need instead.
    subprocess.run(
        [_GODOT, "--headless", "--path", str(dst), "--import"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert (dst / ".godot").is_dir(), "Godot --import did not create the .godot cache"
    return dst


def _run_gdmutant(project: Path, extra: list[str], out: Path) -> dict:
    """Invoke the shipped CLI via subprocess and return the parsed Stryker JSON report."""
    cmd = [
        sys.executable,
        "-m",
        "gdmutant.cli",
        "run",
        str(project / TARGET),
        "--project",
        str(project),
        "--json",
        str(out),
        *extra,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    assert completed.returncode == 0, (
        f"gdmutant exited {completed.returncode}\n--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )
    return json.loads(out.read_text(encoding="utf-8"))


def _assert_pinned_outcomes(report: dict) -> None:
    """Assert the report matches the pinned per-mutant outcomes exactly."""
    files = report["files"]
    assert len(files) == 1, f"expected one file in the report, got {list(files)}"
    (file_obj,) = files.values()
    mutants = file_obj["mutants"]
    counts = Counter(m["status"] for m in mutants)

    # Print the full table on any failure so a red CI run is diagnosable from the log alone.
    table = "\n".join(
        f"  {m['location']['start']['line']}:{m['location']['start']['column']}"
        f"  {m['mutatorName']}  -> {m['replacement']!r}  {m['status']}"
        for m in mutants
    )
    detail = f"\nstatus counts: {dict(counts)}\nmutants:\n{table}"

    assert len(mutants) == EXPECTED_TOTAL, f"mutant count changed{detail}"
    assert counts["CompileError"] == 0, f"a mutant failed to compile (INVALID){detail}"
    assert counts["RuntimeError"] == 0, f"a mutant errored at runtime (ERROR){detail}"
    assert counts["Killed"] == EXPECTED_KILLED, f"killed count changed{detail}"

    survivors = {
        (m["location"]["start"]["line"], m["location"]["start"]["column"], m["replacement"])
        for m in mutants
        if m["status"] == "Survived"
    }
    assert survivors == EXPECTED_SURVIVORS, f"survivor set changed{detail}"


def test_command_runner_against_real_godot(tmp_path: Path) -> None:
    """The CommandRunner path (ADR-0005, exit-code) needs NO addon — so it leads, isolating
    'Godot runs' from 'GdUnit4 runs'. Drives the hand-rolled corpus/harness/run_tests.gd."""
    project = _corpus_copy(tmp_path)
    command = f"{_GODOT} --headless --path . --script res://harness/run_tests.gd"
    report = _run_gdmutant(
        project,
        ["--runner", "command", "--command", command],
        tmp_path / "command_report.json",
    )
    _assert_pinned_outcomes(report)


def test_gdunit4_against_real_godot(tmp_path: Path) -> None:
    """The GdUnit4 path — the real-Godot close: exercises the real ``-s GdUnitCmdTool.gd -a res://test
    -rc 1 --ignoreHeadlessMode`` flags and reads the actual ``reports/report_1/results.xml``."""
    if not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed — run python scripts/install_gdunit4.py")
    project = _corpus_copy(tmp_path)
    report = _run_gdmutant(
        project,
        ["--runner", "gdunit4", "--godot", str(_GODOT)],
        tmp_path / "gdunit_report.json",
    )
    _assert_pinned_outcomes(report)


def test_gut_against_real_godot(tmp_path: Path) -> None:
    """The GUT path — the peer JUnit adapter (ADR-0011): exercises the real ``-s gut_cmdln.gd
    -gdir=res://gut_test -gjunit_xml_file=… -gexit`` flags and reads GUT's actual JUnit report. GUT
    is a *peer* of GdUnit4 over one runner contract, so it must pin the EXACT SAME per-mutant
    outcome (18/11/7 with the identical survivor set) — mutant-for-mutant agreement across the two
    frameworks is the proof the seam is genuinely runner-agnostic, not GdUnit4-shaped."""
    if not GUT_ADDON.is_dir():
        pytest.skip("GUT addon not installed — run python scripts/install_gut.py")
    project = _corpus_copy(tmp_path)
    report = _run_gdmutant(
        project,
        ["--runner", "gut", "--tests", "res://gut_test", "--godot", str(_GODOT)],
        tmp_path / "gut_report.json",
    )
    _assert_pinned_outcomes(report)


# An uncompilable target (a parse gdtoolkit would reject too, but here it's the file *under test*,
# not a mutant): keeps `class_name TurnOrder` so the TurnOrder-referencing suite still resolves the
# name yet fails to load, while that framework's independent suite stays healthy. Shared by both
# crash-safety probes below, so GUT and GdUnit4 are driven with the identical break.
_UNCOMPILABLE_TARGET = "class_name TurnOrder\nextends RefCounted\nfunc broken( ->:\n"


def test_gut_crash_safety_never_reports_a_false_survivor_at_n_gt_1(tmp_path: Path) -> None:
    """Crash-safety at **n>1** (ADR-0011) — the probe the single-file corpus could never run.

    The `tests == 0 → error` guard is only meaningful if a compile crash actually zeroes the run.
    The corpus's lone TurnOrder-referencing GUT suite guarantees that (breaking turn_order.gd breaks
    the only suite), so it proves the guard at n=1 only. A REAL multi-file suite is the risk: if a
    mutant breaks just the file(s) referencing the mutated source and GUT skips the broken file and
    runs the rest, the report carries the healthy files' green tests → a PASS → SURVIVED, a false
    survivor straight through the `tests == 0` guard.

    This drives that exact shape against real GUT, exactly as the engine would: a **healthy baseline
    run first** (which fixes the runner's expected test count), then — with a SECOND, independent
    suite (``test_independent_gut.gd``) that compiles and passes on its own — turn_order.gd is made
    uncompilable and the SAME runner is run again (the mutant scenario). The invariant is **never a
    false survivor** — the mutant run must come back a **kill** (``failures``/``errors`` > 0) or an
    **error** (the guard raises), but **never a passing `SuiteResult`**. It records which branch
    real GUT took (abort-all vs skip-and-continue vs run-and-fail) so CI documents the behavior.

    Real GUT v9.7.1 **skips-and-continues** (the broken suite is skipped, the healthy suite runs
    green), so ``tests == 0`` alone would NOT catch it — the baseline-test-count-drop guard is what
    surfaces it as an error (see `GutRunner`).
    """
    if not GUT_ADDON.is_dir():
        pytest.skip("GUT addon not installed — run python scripts/install_gut.py")
    from gdmutant.adapters.gdscript.runner import GutRunner

    project = _corpus_copy(tmp_path)
    # Sanity: the second, independent suite is present, so this is genuinely an n>1 run.
    assert (project / "gut_test" / "test_independent_gut.gd").is_file()

    runner = GutRunner(test_dir="res://gut_test", godot=str(_GODOT))
    # 1. Healthy baseline (as the engine runs first): every suite loads, fixing the expected count.
    baseline = runner.run(str(project))
    assert baseline.passed and baseline.tests >= 5, (
        f"the healthy GUT baseline should pass with both suites loaded, got {baseline}"
    )

    # 2. Break the source-under-test and run the SAME runner again (the mutant scenario).
    (project / TARGET).write_text(_UNCOMPILABLE_TARGET, encoding="utf-8")
    branch: str
    result: SuiteResult | None = None
    try:
        result = runner.run(str(project))
    except RuntimeError as error:
        branch = f"ERROR — the guard raised (zero-test or test-count drop): {error}"
    else:
        if result.failed:
            branch = (
                f"KILLED — GUT ran the broken suite and it failed at runtime "
                f"(tests={result.tests}, failures={result.failures}, errors={result.errors})"
            )
        else:
            branch = (
                f"FALSE SURVIVOR — GUT skipped the broken suite and passed the rest "
                f"(tests={result.tests}, failures={result.failures}, errors={result.errors})"
            )

    print(f"\n[GUT crash-safety probe] real GUT branch: {branch}")
    # The one outcome that must never happen: a clean pass off the healthy suite alone.
    assert result is None or result.failed, (
        "GUT reported a PASS for an uncompilable source-under-test at n>1 — a false survivor. "
        f"The baseline-test-count-drop guard failed to fire. Observed: {branch}"
    )


def test_gdunit4_crash_safety_never_reports_a_false_survivor_at_n_gt_1(tmp_path: Path) -> None:
    """The GdUnit4 peer of the GUT probe above — and the reason ADR-0011's claim is no longer n=1.

    GdUnit4 is gdmutant's **default** runner, and it used to override nothing for crash safety: its
    whole defence was the base's "the report must reappear" guard, justified in ADR-0011 with "a
    crash writes no report". That was only ever observed against the corpus's single suite, where
    breaking ``turn_order.gd`` breaks the only suite — n=1. GUT looked exactly as safe at n=1 and
    turned out to skip-and-continue, which is a false survivor. Assuming GdUnit4 differs, on a
    single observation, on the default path, is precisely the bet that is not worth making.

    So this drives the same shape GUT gets: a **healthy baseline first**, then — with a SECOND,
    independent suite (``test_independent.gd``) that compiles and passes on its own —
    ``turn_order.gd`` is made uncompilable and the SAME runner is run again. The invariant is
    **never a false survivor**: a kill (``failures``/``errors`` > 0) or an error (a guard raises),
    but never a passing `SuiteResult`. It records which branch real GdUnit4 took so CI documents the
    behavior instead of the docs asserting it.

    Observed (GdUnit4 v6.1.3, Godot 4.7): **abort-at-discovery.** GdUnit4 loads every suite up
    front, so the unparseable one aborts the whole run — "Script errors were detected during test
    discovery!", exit 105, no report written, and the healthy suite never ran. Unlike GUT, there is
    no healthy-suites-green report for a drop guard to measure, which is why `GdUnit4Runner` has
    none. If that ever changes, this probe is what fails first.
    """
    if not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed — run python scripts/install_gdunit4.py")
    from gdmutant.adapters.gdscript.runner import GdUnit4Runner

    project = _corpus_copy(tmp_path)
    # Sanity: the second, independent suite is present, so this is genuinely an n>1 run.
    assert (project / "test" / "test_independent.gd").is_file()

    runner = GdUnit4Runner(test_path="res://test", godot=str(_GODOT))
    # 1. Healthy baseline (as the engine runs first): every suite loads and passes.
    baseline = runner.run(str(project))
    assert baseline.passed and baseline.tests >= 5, (
        f"the healthy GdUnit4 baseline should pass with both suites loaded, got {baseline}"
    )

    # 2. Break the source-under-test and run the SAME runner again (the mutant scenario).
    (project / TARGET).write_text(_UNCOMPILABLE_TARGET, encoding="utf-8")
    branch: str
    result: SuiteResult | None = None
    try:
        result = runner.run(str(project))
    except RuntimeError as error:
        branch = f"ERROR — a guard raised (no report, or a zero-test report): {error}"
    else:
        if result.failed:
            branch = (
                f"KILLED — GdUnit4 ran the broken suite and it failed "
                f"(tests={result.tests}, failures={result.failures}, errors={result.errors})"
            )
        else:
            branch = (
                f"FALSE SURVIVOR — GdUnit4 skipped the broken suite and passed the rest "
                f"(tests={result.tests}, failures={result.failures}, errors={result.errors})"
            )

    print(f"\n[GdUnit4 crash-safety probe] real GdUnit4 branch: {branch}")
    # The one outcome that must never happen: a clean pass off the healthy suite alone.
    assert result is None or result.failed, (
        "GdUnit4 reported a PASS for an uncompilable source-under-test at n>1 — a false survivor, "
        f"on the DEFAULT runner. Observed: {branch}"
    )


# GDScript has no exceptions (engine.runner._SCRIPT_ERROR_MARKER's docstring): an out-of-bounds
# array access aborts only the CURRENT FUNCTION CALL at that statement and the call returns the
# declared return type's default -- bool's default is `false`. silently_wrong() never reaches
# `return true`. A test that happens to assert `is_false()`/`assert_false()` on the result would
# see a normal, passing assertion despite the function never completing -- a per-test-method
# sibling of stryker-js#6150 (a mutant that broke test collection was scored Survived because the
# runner only read recorded assertion results, never "did the code under test actually run").
# Referenced via `preload`, not `class_name`, so this probe needs no Godot import-cache warm-up:
# the file is added to the project copy AFTER `_corpus_copy`'s one-time `--import` scan runs.
_SILENTLY_WRONG_LIB = """extends RefCounted

static func silently_wrong() -> bool:
	var empty: Array = []
	var _boom = empty[0]  # out-of-bounds -> runtime SCRIPT ERROR; the call aborts HERE
	return true  # never reached if the abort above is real
"""

_GDUNIT_SCRIPT_ERROR_PROBE_SUITE = """extends GdUnitTestSuite

func test_masked_by_default_return() -> void:
	var lib = preload("res://silently_wrong_lib.gd")
	assert_bool(lib.silently_wrong()).is_false()
"""

_GUT_SCRIPT_ERROR_PROBE_SUITE = """extends GutTest

func test_masked_by_default_return():
	var lib = preload("res://silently_wrong_lib.gd")
	assert_false(lib.silently_wrong())
"""


def test_gdunit4_runtime_script_error_is_never_a_silent_pass(tmp_path: Path) -> None:
    """Pins the crash-safety property one level BELOW "zero tests collected" (the two probes
    above, and `GdUnit4Runner`'s class docstring): a test whose suite loads fine and whose own
    assertion nominally passes, but only because the assertion happens to match the default
    value a runtime-aborted function call returns. See the fixtures' comment for why that
    coincidence is possible at all.

    The only thing standing between this and a false SURVIVED is GdUnit4's OWN runtime-error
    interception (`GodotGdErrorMonitor`, gated by the `gdunit4/settings/report/godot/script_error`
    project setting, which `GdUnitSettings.is_report_script_errors()` defaults to `true`) turning
    the SCRIPT ERROR into a failure report before gdmutant ever reads the JUnit XML. gdmutant
    passes no flag that touches this setting, so a future GdUnit4 release shipping that default
    off would silently flip this from a kill to a false survivor with no change to gdmutant's own
    code -- this test is what would go red first.
    """
    if not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed — run python scripts/install_gdunit4.py")
    from gdmutant.adapters.gdscript.runner import GdUnit4Runner

    project = _corpus_copy(tmp_path)
    (project / "silently_wrong_lib.gd").write_text(_SILENTLY_WRONG_LIB, encoding="utf-8")
    suite_dir = project / "test_script_error_probe"
    suite_dir.mkdir()
    (suite_dir / "test_silent_script_error.gd").write_text(
        _GDUNIT_SCRIPT_ERROR_PROBE_SUITE, encoding="utf-8"
    )

    runner = GdUnit4Runner(test_path="res://test_script_error_probe", godot=str(_GODOT))
    result = runner.run(str(project))
    assert result.failed, (
        "GdUnit4 reported a PASS for a runtime SCRIPT ERROR whose aborted-call default happened "
        f"to satisfy the test's own assertion — a false survivor: {result}"
    )
    assert result.tests == 1, f"expected exactly the probe's one test, got {result}"


def test_gut_runtime_script_error_is_never_a_silent_pass(tmp_path: Path) -> None:
    """The GUT peer of the GdUnit4 probe above — same question, same fixtures.

    GUT's own runtime-error interception (`GutErrorTracker`, installed via `OS.add_logger` —
    `corpus/addons/gut/error_tracker.gd`) fails a test that produced an unhandled engine/script
    error even when the test's own assertion passed, gated by `-gfailure_error_types` defaulting
    to include `engine` (`gut_config.gd`'s `failure_error_types` default). gdmutant passes no
    `-gfailure_error_types` override, so it relies entirely on that shipped default; if a future
    GUT release drops `engine` from it, this is what would go red first — before a real mutation
    run started scoring runtime-broken mutants as caught.
    """
    if not GUT_ADDON.is_dir():
        pytest.skip("GUT addon not installed — run python scripts/install_gut.py")
    from gdmutant.adapters.gdscript.runner import GutRunner

    project = _corpus_copy(tmp_path)
    (project / "silently_wrong_lib.gd").write_text(_SILENTLY_WRONG_LIB, encoding="utf-8")
    suite_dir = project / "gut_test_script_error_probe"
    suite_dir.mkdir()
    (suite_dir / "test_silent_script_error_gut.gd").write_text(
        _GUT_SCRIPT_ERROR_PROBE_SUITE, encoding="utf-8"
    )

    runner = GutRunner(test_dir="res://gut_test_script_error_probe", godot=str(_GODOT))
    result = runner.run(str(project))
    assert result.failed, (
        "GUT reported a PASS for a runtime SCRIPT ERROR whose aborted-call default happened to "
        f"satisfy the test's own assertion — a false survivor: {result}"
    )
    assert result.tests == 1, f"expected exactly the probe's one test, got {result}"


def test_gdunit4_report_cleanup_never_touches_an_unrelated_directory(tmp_path: Path) -> None:
    """GdUnit4's own end-of-session cleanup (``cleanup_report_history``) deletes any directory under
    the report base path whose name starts with ``report_``, reading whatever follows the prefix
    as an integer via GDScript's ``String.to_int()`` — which silently returns ``0`` for non-numeric
    text instead of erroring. A directory that merely shares the prefix (some other tool's output, a
    manually named folder) is therefore treated as index 0 and deleted alongside GdUnit4's own old
    numbered reports, confirmed directly against real GdUnit4 v6.1.3 source.

    ``GdUnit4Runner.command``'s ``-rc 1`` flag happens to make this unreachable here: GdUnit4's own
    ``current_report_history_index`` getter is hardcoded to ``1`` whenever ``max_report_history`` is
    not greater than 1, skipping the directory scan entirely, so the cleanup threshold is always
    ``1 - 1 - 1 == -1`` and nothing can ever satisfy the deletion check. That is an accident of
    GdUnit4's own branching, not a documented guarantee, so this pins it directly against the real
    runner: an unrelated ``report_``-prefixed directory sitting next to gdmutant's own report must
    survive real, repeated GdUnit4 runs (one per mutant, in practice) untouched.
    """
    if not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed — run python scripts/install_gdunit4.py")
    from gdmutant.adapters.gdscript.runner import GdUnit4Runner

    project = _corpus_copy(tmp_path)
    unrelated = project / "reports" / "report_coverage_html"
    unrelated.mkdir(parents=True)
    marker = unrelated / "marker.txt"
    marker.write_text("not a GdUnit4 report — some other tool's output", encoding="utf-8")

    runner = GdUnit4Runner(test_path="res://test", godot=str(_GODOT))
    # Run it more than once: gdmutant calls this once per mutant, so the property must hold across
    # repeated invocations against the same project, not just the first.
    for _ in range(2):
        result = runner.run(str(project))
        assert result.passed

    assert unrelated.is_dir(), "an unrelated report_-prefixed directory was deleted"
    assert marker.is_file(), "the unrelated directory's contents were deleted"


def test_gdunit4_empty_discovery_is_diagnosed_as_discovery_not_a_crash(tmp_path: Path) -> None:
    """A wrong ``--tests`` path must not be reported as a Godot crash.

    GdUnit4 exits **0** and writes no report when discovery finds no suites, so the base runner's
    "wrote no report — Godot may have failed to run" sends the user to debug a crash that is not
    happening. This pins that `GdUnit4Runner` reads GdUnit4's own "No test cases found" and says
    what to fix instead — against the real binary, because the whole hint hangs off a string only
    real GdUnit4 emits.
    """
    if not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed — run python scripts/install_gdunit4.py")
    from gdmutant.adapters.gdscript.runner import GdUnit4Runner

    project = _corpus_copy(tmp_path)
    # res://harness holds the CommandRunner harness script — real, but not a GdUnit4 suite.
    runner = GdUnit4Runner(test_path="res://harness", godot=str(_GODOT))
    with pytest.raises(RuntimeError) as excinfo:
        runner.run(str(project))
    message = str(excinfo.value)
    assert "discovered no test suites" in message, message
    assert "--tests" in message, message
    # Never the addon hint's trigger phrase: the addon plainly loaded, it just found nothing.
    assert "wrote no report" not in message, message


def test_statement_deletion_mutants_all_compile_in_godot(tmp_path: Path) -> None:
    """ADR-0007's falsifiable check: every statement-deletion mutant gdmutant emits for the corpus
    must actually *load* in Godot. gdtoolkit has no return-path analysis, so if the generation-time
    return-guard is ever unsound, a deletion would fail to compile here — the exact failure the
    guard exists to prevent (a mutant that hangs one runner / errors the other). `--check-only`
    exits 0 even on a parse error, so the assertion scrapes stderr."""
    from gdmutant.adapters.gdscript import generate_mutants

    source = (CORPUS / TARGET).read_text(encoding="utf-8")
    deletions = [
        m
        for m in generate_mutants(str(CORPUS / TARGET), source)
        if m.operator_id == "statement-deletion"
    ]
    assert deletions, "the corpus should exercise the statement-deletion operator"
    for m in deletions:
        script = tmp_path / f"del_{m.span.line}.gd"
        script.write_text(m.apply(source), encoding="utf-8")
        result = subprocess.run(
            [str(_GODOT), "--headless", "--check-only", "--script", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "Parse Error" not in result.stderr and "SCRIPT ERROR" not in result.stderr, (
            f"statement-deletion at line {m.span.line} does not compile in Godot — the return-path "
            f"guard is unsound (ADR-0007):\n{result.stderr[-600:]}"
        )


# An adversarial source the *corpus* doesn't contain: a typed lambda (whose sole return, if deleted,
# is a Godot "not all code paths return a value" error), plus the cases the guard must still allow.
# The corpus has no typed lambda, so without this the live oracle would never exercise that path.
_TYPED_LAMBDA_SOURCE = """extends Node


func with_typed_lambda() -> void:
	var typed := func() -> int:
		return 9
	var untyped := func():
		return 7
	print(typed.call() + untyped.call())


func typed_with_backstop(a: int) -> int:
	if a < 0:
		return 0
	return a
"""


def test_typed_lambda_return_deletion_is_guarded_and_emitted_deletions_compile(
    tmp_path: Path,
) -> None:
    """Closes the typed-lambda gap: a `lambda_header` carries the same `-> TYPE_HINT` as a
    function, so a typed lambda's return is a return-value requirement Godot enforces. Assert the
    guard never emits that sole return, and that every deletion it *does* emit for this adversarial
    source loads clean in real Godot (the untyped lambda's return and the backstopped early one)."""
    from gdmutant.adapters.gdscript import generate_mutants

    script_path = tmp_path / "typed_lambda.gd"
    deletions = [
        m
        for m in generate_mutants(str(script_path), _TYPED_LAMBDA_SOURCE)
        if m.operator_id == "statement-deletion"
    ]
    # The typed lambda's sole `return 9` must never be a deletion target.
    assert not any(m.original == "return 9" for m in deletions), (
        "the typed lambda's sole return was emitted — the guard is unsound"
    )
    # The untyped lambda's return and the typed function's backstopped early return are allowed.
    emitted = {m.original for m in deletions}
    assert "return 7" in emitted and "return 0" in emitted
    # Every emitted deletion must actually load in Godot.
    for m in deletions:
        script = tmp_path / f"typed_lambda_del_{m.span.line}.gd"
        script.write_text(m.apply(_TYPED_LAMBDA_SOURCE), encoding="utf-8")
        result = subprocess.run(
            [str(_GODOT), "--headless", "--check-only", "--script", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "Parse Error" not in result.stderr and "SCRIPT ERROR" not in result.stderr, (
            f"emitted deletion at line {m.span.line} does not compile in Godot:\n"
            f"{result.stderr[-600:]}"
        )


def test_command_harness_fails_fast_on_an_uncompilable_target(tmp_path: Path) -> None:
    """A mutant that makes the target uncompilable must make the CommandRunner harness exit
    NON-ZERO promptly — not exit 0 (a false PASS that would let a broken mutant survive) and not
    hang. The reference harness gates on ``GDScript.can_instantiate()`` before calling the target.
    The ``timeout=`` here doubles as the hang guard: a hang raises TimeoutExpired and fails."""
    project = _corpus_copy(tmp_path)
    harness = ["--headless", "--path", str(project), "--script", "res://harness/run_tests.gd"]

    healthy = subprocess.run([str(_GODOT), *harness], capture_output=True, text=True, timeout=60)
    assert healthy.returncode == 0, f"the healthy harness should pass:\n{healthy.stderr[-600:]}"

    # Overwrite the target with a Godot compile error (a parse gdtoolkit accepts but Godot won't).
    (project / TARGET).write_text(
        "class_name TurnOrder\nextends RefCounted\nfunc broken( ->:\n", encoding="utf-8"
    )
    broken = subprocess.run([str(_GODOT), *harness], capture_output=True, text=True, timeout=60)
    assert broken.returncode != 0, (
        "the harness exited 0 on an uncompilable target — a false PASS:\n"
        f"{(broken.stdout + broken.stderr)[-600:]}"
    )


def test_the_benchmarks_real_godot_scenario_does_the_documented_work() -> None:
    """`scripts/benchmark.py`'s opt-in `godot-corpus` scenario is the realistic end-to-end number.

    It lives here rather than in `tests/test_benchmark.py` because it launches Godot once per
    mutant, and this file is the one the mutation sweep leaves out. Only the work is asserted, the
    same pinned total and kill count as the runner tests above, never the time."""
    import importlib.util

    script = REPO_ROOT / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("benchmark", script)
    assert spec and spec.loader
    benchmark = importlib.util.module_from_spec(spec)
    sys.modules["benchmark"] = benchmark
    spec.loader.exec_module(benchmark)

    result = benchmark.measure_godot_corpus(str(_GODOT), repeat=1)
    assert (result.mutants, result.killed) == (EXPECTED_TOTAL, EXPECTED_KILLED)
    assert len(result.times) == 1


# Marker placement (docs/decisions/0017, Plan step 1). The ADR's bar for step 1: every marked
# corpus file parses, keeps its error line numbers, and every spot that can take a marker fires.
# Only Godot can show any of that, so these mark the corpus in a throwaway copy and run it.
#
# Nothing records hits in gdmutant yet: that is step 2. So these tests bring a stand-in recorder.
# `_GdmMarks` is a ``class_name`` script with a static ``hit``, which the marker text
# ``_GdmMarks.hit(N); `` calls exactly as it would call an autoload of that name. It is a class and
# not the autoload the ADR describes because the corpus's command harness loads the code under test
# in its ``_init``, before Godot has registered any autoload. A marker naming an autoload fails to
# compile there ("Identifier not found"), while a global class resolves. A small autoload writes the
# hits to a file when Godot exits.

_MARKER_HITS = "_gdm_hits.json"

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
\t\tvar out := FileAccess.open("res://{_MARKER_HITS}", FileAccess.WRITE)
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


def _marker_project(tmp_path: Path, name: str, extra: dict[str, str], marked: bool) -> Path:
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


def _marked_corpus_file(relative: str) -> MarkedSource:
    source = (CORPUS / relative).read_text(encoding="utf-8")
    return place_markers(source, generate_mutants(relative, source))


def _run_scene(project: Path, script: str) -> str:
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


def _marker_hits(project: Path) -> set[int]:
    """The spots the last run recorded. The file is written at exit even when nothing was hit,
    so a missing file means the writer never ran, never that no spot fired."""
    path = project / _MARKER_HITS
    assert path.is_file(), f"no {_MARKER_HITS}: the hit writer did not run"
    hits = {int(spot) for spot in json.loads(path.read_text(encoding="utf-8"))}
    path.unlink()
    return hits


def _marker_runners() -> list[tuple[str, Runner]]:
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
    runners = dict(_marker_runners())
    if runner_name not in runners:
        pytest.skip(f"{runner_name} addon not installed")
    runner = runners[runner_name]
    results: dict[bool, SuiteResult] = {}
    hits: dict[bool, set[int]] = {}
    for marked in (False, True):
        project = _marker_project(tmp_path, f"{runner_name}-{marked}", {}, marked)
        results[marked] = runner.run(str(project))
        hits[marked] = _marker_hits(project)
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
    spots = {spot.line: spot.id for spot in _marked_corpus_file("turn_order.gd").spots}
    untested = {spots[27], spots[32]}
    assert hits[True] == set(spots.values()) - untested


def test_every_spot_fires_when_every_function_is_called(tmp_path: Path) -> None:
    project = _marker_project(tmp_path, "all", {"_call.gd": _CALL_EVERYTHING}, marked=True)
    output = _run_scene(project, "res://_call.gd")
    assert "SCRIPT ERROR" not in output, output
    result = _marked_corpus_file("turn_order.gd")
    assert not any(isinstance(p, RunEverything) for p in result.placements)
    assert _marker_hits(project) == {spot.id for spot in result.spots}


def test_an_elif_condition_marker_fires_on_a_path_that_skips_the_elif_body(
    tmp_path: Path,
) -> None:
    project = _marker_project(
        tmp_path, "elif", {"grade.gd": _GRADE, "_call.gd": _CALL_GRADE_50}, marked=True
    )
    output = _run_scene(project, "res://_call.gd")
    assert "GRADE|C" in output, output
    mutants = generate_mutants("grade.gd", _GRADE)
    result = place_markers(_GRADE, mutants)
    (elif_spot,) = {
        placement
        for mutant, placement in zip(mutants, result.placements, strict=True)
        if mutant.span.line == 5 and mutant.original == ">"
    }
    body_spot = next(spot.id for spot in result.spots if spot.line == 6)
    hits = _marker_hits(project)
    assert elif_spot in hits
    assert body_spot not in hits  # the path really did skip the elif body


def test_marked_code_reports_errors_on_the_same_lines(tmp_path: Path) -> None:
    files = {"boom.gd": _RUNTIME_ERROR, "bad.gd": _COMPILE_ERROR, "_call.gd": _CALL_BOOM}
    lines: dict[bool, list[tuple[str, str]]] = {}
    for marked in (False, True):
        project = _marker_project(tmp_path, f"errors-{marked}", files, marked)
        output = _run_scene(project, "res://_call.gd")
        lines[marked] = re.findall(r"res://(boom|bad)\.gd:(\d+)", output)
    # Both errors really happened, on the lines the fixtures put them on.
    assert ("boom", "7") in lines[False]
    assert ("bad", "4") in lines[False]
    assert lines[True] == lines[False]
    # And the marked copy did put a marker on both of those lines.
    for name, text, line in (("boom.gd", _RUNTIME_ERROR, 7), ("bad.gd", _COMPILE_ERROR, 4)):
        spots = place_markers(text, generate_mutants(name, text)).spots
        assert line in {spot.line for spot in spots}


# Coverage analysis (docs/decisions/0017, Plan step 2): the marker run and the "no coverage"
# verdict, through the shipped CLI and the engine, against real Godot. The bar is the ADR's
# two-sided evidence. With markers on, every mutant must get the verdict it gets with markers off,
# except that "no coverage" may stand in for "survived" and nothing else. And a broken map, or a
# recorder that never ran, must be caught rather than reported.

_GODOT_EXE = _GODOT or ""


def _coverage_project(
    tmp_path: Path, name: str, extra: dict[str, str], autoloads: str = ""
) -> Path:
    """A corpus copy with `extra` files and any `autoloads` lines added, then imported."""
    project = tmp_path / name
    shutil.copytree(CORPUS, project, ignore=shutil.ignore_patterns(".godot", "reports"))
    for relative, text in extra.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    if autoloads:
        settings = project / "project.godot"
        settings.write_text(
            settings.read_text(encoding="utf-8") + f"\n[autoload]\n\n{autoloads}\n",
            encoding="utf-8",
            newline="\n",
        )
    subprocess.run(
        [_GODOT_EXE, "--headless", "--path", str(project), "--import"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert (project / ".godot").is_dir(), "Godot --import did not create the .godot cache"
    return project


def _runner_args(runner: str, tests: str | None = None) -> list[str]:
    if runner == "command":
        return [
            "--runner",
            "command",
            "--command",
            f"{_GODOT_EXE} --headless --path . --script res://harness/run_tests.gd",
            "--godot",
            _GODOT_EXE,
        ]
    if runner == "gut":
        return ["--runner", "gut", "--tests", tests or "res://gut_test", "--godot", _GODOT_EXE]
    return ["--runner", "gdunit4", "--tests", tests or "res://test", "--godot", _GODOT_EXE]


def _gdmutant(
    project: Path, target: str, extra: list[str], out: Path
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    """Run the shipped CLI and return the process and the JSON report (None if none was written)."""
    cmd = [sys.executable, "-m", "gdmutant.cli", "run", str(project / target)]
    cmd += ["--project", str(project), "--json", str(out), *extra]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    return done, report


def _statuses(report: dict) -> dict[tuple[int, int, str, str], str]:
    ((_, entry),) = report["files"].items()
    return {
        (
            m["location"]["start"]["line"],
            m["location"]["start"]["column"],
            m["mutatorName"],
            m["replacement"],
        ): m["status"]
        for m in entry["mutants"]
    }


def _assert_two_sided(off: dict, on: dict) -> set[tuple[int, int, str, str]]:
    """Every mutant's verdict with markers on equals its verdict with markers off, except that a
    mutant reported NoCoverage must have SURVIVED with markers off. Returns the NoCoverage keys."""
    before, after = _statuses(off), _statuses(on)
    assert before.keys() == after.keys(), "markers changed which mutants exist"
    uncovered = {key for key, status in after.items() if status == "NoCoverage"}
    for key, status in after.items():
        if key in uncovered:
            assert before[key] == "Survived", (
                f"{key} is NoCoverage with markers on but {before[key]} with markers off: a "
                "mutant a real run treats differently was hidden"
            )
        else:
            assert status == before[key], f"{key}: {before[key]} off, {status} on"
    return uncovered


def _both_ways(
    tmp_path: Path, project: Path, target: str, args: list[str]
) -> tuple[dict, dict, str, dict[str, float]]:
    """Run gdmutant on `target` with markers off, then on. Returns both reports, the markers-on
    stdout, and the wall-clock of each run."""
    times: dict[str, float] = {}
    reports: dict[str, dict] = {}
    stdout = ""
    for mode in ("off", "all"):
        started = time.monotonic()
        done, report = _gdmutant(
            project, target, [*args, "--coverage-analysis", mode], tmp_path / f"{mode}.json"
        )
        times[mode] = time.monotonic() - started
        assert done.returncode == 0, f"{mode}: exit {done.returncode}\n{done.stdout}\n{done.stderr}"
        assert report is not None
        reports[mode] = report
        stdout = done.stdout
    return reports["off"], reports["all"], stdout, times


@pytest.mark.parametrize("runner", ["command", "gdunit4", "gut"])
def test_coverage_analysis_gives_every_mutant_the_verdict_it_gets_without_it(
    tmp_path: Path, runner: str
) -> None:
    if runner == "gdunit4" and not ADDON.is_dir():
        pytest.skip("GdUnit4 addon not installed")
    if runner == "gut" and not GUT_ADDON.is_dir():
        pytest.skip("GUT addon not installed")
    project = _corpus_copy(tmp_path)
    off, on, stdout, times = _both_ways(tmp_path, project, TARGET, _runner_args(runner))
    uncovered = _assert_two_sided(off, on)
    # Not vacuous: can_act and ties_favor_earlier are untested on purpose, so their three mutants
    # are exactly the ones no test reaches, and every one of them is self-checked.
    assert {(line, col) for line, col, _, _ in uncovered} == {(27, 15), (27, 19), (32, 9)}
    assert (
        "Coverage self-check: re-ran 3 of the 3 no-coverage mutants against the whole suite"
        in stdout
    )
    print(f"\n{runner}: markers off {times['off']:.1f}s, markers on {times['all']:.1f}s")


class _Sabotaged:
    """Wraps the real marker, then breaks the copy after marking: the ADR's "a hook that never
    fires, a dropped credit". `drop_spot` makes the recorder ignore one spot, `no_writer` removes
    the autoload that writes the hits file."""

    def __init__(self, drop_spot: int | None = None, no_writer: bool = False) -> None:
        self.inner = GDScriptMarker(godot=_GODOT_EXE)
        self.drop_spot = drop_spot
        self.no_writer = no_writer

    def mark(self, copy_dir: str, files: Any) -> MarkedCopy:
        marked = self.inner.mark(copy_dir, files)
        copy = Path(copy_dir)
        if self.drop_spot is not None:
            marks = copy / RECORDER_DIR / "marks.gd"
            text = marks.read_text(encoding="utf-8")
            broken = text.replace(
                "static func _record(spot: int) -> void:\n\tlast[spot] = window",
                "static func _record(spot: int) -> void:\n"
                f"\tif spot == {self.drop_spot}:\n"
                "\t\treturn\n"
                "\tlast[spot] = window",
            )
            assert broken != text, "the recorder no longer has the shape this sabotage edits"
            marks.write_text(broken, encoding="utf-8", newline="\n")
        if self.no_writer:
            settings = copy / "project.godot"
            kept = [
                line
                for line in settings.read_text(encoding="utf-8").split("\n")
                if not line.startswith(WRITER_AUTOLOAD)
            ]
            settings.write_text("\n".join(kept), encoding="utf-8", newline="\n")
        return marked


def _engine_run(project: Path, marker: _Sabotaged, self_check: int | None) -> MutationRun:
    harness = [_GODOT_EXE, "--headless", "--path", ".", "--script", "res://harness/run_tests.gd"]
    target = project / TARGET
    return engine_run(
        str(project),
        str(target),
        target.read_text(encoding="utf-8"),
        CommandRunner(command=harness),
        ADAPTER,
        coverage=CoverageAnalysis.ALL,
        marker=marker,
        self_check=self_check,
    )


def test_a_map_missing_one_hit_is_caught_by_the_self_check(tmp_path: Path) -> None:
    project = _corpus_copy(tmp_path)
    # The first spot is acts_before's `return`, whose `>` -> `>=` mutant the suite kills. Dropping
    # its hit makes the map call a killable mutant "no coverage".
    spots = _marked_corpus_file(TARGET).spots
    assert spots[0].line == 8
    with pytest.raises(CoverageSelfCheckFailed, match=r"turn_order\.gd:8:.*gave 'killed'"):
        _engine_run(project, _Sabotaged(drop_spot=spots[0].id), self_check=None)
    # And the project was left exactly as it was.
    assert (project / TARGET).read_text(encoding="utf-8") == (CORPUS / TARGET).read_text(
        encoding="utf-8"
    )


def test_a_recorder_that_never_writes_its_hits_fails_the_marker_run(tmp_path: Path) -> None:
    project = _corpus_copy(tmp_path)
    with pytest.raises(CoverageRunFailed, match="wrote no hits file"):
        _engine_run(project, _Sabotaged(no_writer=True), self_check=None)


# Code that runs at load time, from an autoload's `_init`, is reached before any test, so its
# mutants are never "no coverage". A class-level `var` cannot take a marker at all, so it always
# runs. Only `_unused`, which nothing calls, is unreached. No test checks any of it, so with markers
# off every mutant here survives, and the two-sided check pins what markers on may change.
_BOOT = """extends Node

var booted := 10


func _init() -> void:
\tbooted = _compute(4)


func _compute(n: int) -> int:
\treturn n * 3


func _unused() -> int:
\treturn 5
"""


def test_load_time_code_is_reached_and_class_level_code_always_runs(tmp_path: Path) -> None:
    project = _coverage_project(
        tmp_path, "boot", {"boot.gd": _BOOT}, autoloads='Boot="*res://boot.gd"'
    )
    off, on, _, _ = _both_ways(tmp_path, project, "boot.gd", _runner_args("command"))
    uncovered = _assert_two_sided(off, on)
    assert uncovered, "nothing was uncovered, so the markers never ran"
    assert {line for line, _, _, _ in uncovered} == {15}  # `_unused` alone
    class_level = {key: s for key, s in _statuses(on).items() if key[0] == 3}
    assert class_level and set(class_level.values()) == {"Survived"}  # it ran the whole suite


# Deferred code: a `call_deferred` a test awaits, and a timer one test file starts without waiting
# that a later file observes. Both are killable only if the map credits them, so neither may be
# "no coverage". A hit anywhere in the marker run counts as reached in step 2 (per-file windows,
# where crossing files matters, are step 3).
_DEFERRED = """class_name Deferred
extends RefCounted

static var value := 0
static var ticks := 0


static func start() -> void:
\t_fire.call_deferred()


static func _fire() -> void:
\tvalue = 7


static func start_timer() -> void:
\tvar tree := Engine.get_main_loop() as SceneTree
\ttree.create_timer(0.05).timeout.connect(_tick)


static func _tick() -> void:
\tticks = 3
"""
_DEFERRED_GDUNIT = {
    "deferred_test/test_a_deferred.gd": """extends GdUnitTestSuite


func test_a_deferred_call_lands() -> void:
\tDeferred.start()
\tawait get_tree().process_frame
\tassert_int(Deferred.value).is_equal(7)


func test_b_starts_a_timer_it_does_not_wait_for() -> void:
\tDeferred.start_timer()
""",
    # The observer starts the timer itself if nothing has yet. GdUnit4's discovery order is not
    # alphabetical on every platform (Linux runs these two the other way round), so a file that
    # only ever observes would fail outright whenever it happened to run first.
    "deferred_test/test_b_observer.gd": """extends GdUnitTestSuite


func test_the_timer_started_in_another_file_fired() -> void:
\tif Deferred.ticks == 0:
\t\tDeferred.start_timer()
\tawait get_tree().create_timer(0.5).timeout
\tassert_int(Deferred.ticks).is_equal(3)
""",
}
_DEFERRED_GUT = {
    "deferred_gut/test_a_deferred.gd": """extends GutTest


func test_a_deferred_call_lands() -> void:
\tDeferred.start()
\tawait wait_frames(1)
\tassert_eq(Deferred.value, 7)


func test_b_starts_a_timer_it_does_not_wait_for() -> void:
\tDeferred.start_timer()
\tpass_test("the timer is observed by another file")
""",
    "deferred_gut/test_b_observer.gd": """extends GutTest


func test_the_timer_started_in_another_file_fired() -> void:
\tif Deferred.ticks == 0:
\t\tDeferred.start_timer()
\tawait wait_seconds(0.5)
\tassert_eq(Deferred.ticks, 3)
""",
}


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_deferred_code_a_test_observes_is_never_no_coverage(tmp_path: Path, runner: str) -> None:
    if not (ADDON if runner == "gdunit4" else GUT_ADDON).is_dir():
        pytest.skip(f"{runner} addon not installed")
    tests = _DEFERRED_GDUNIT if runner == "gdunit4" else _DEFERRED_GUT
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(tmp_path, "deferred", {"deferred.gd": _DEFERRED, **tests})
    off, on, _, _ = _both_ways(tmp_path, project, "deferred.gd", _runner_args(runner, test_dir))
    _assert_two_sided(off, on)
    statuses = _statuses(on)
    # value = 7 in _fire, and ticks = 3 in _tick: killed both ways, never hidden.
    for line in (13, 22):
        verdicts = {s for (ln, _, _, _), s in statuses.items() if ln == line}
        assert verdicts == {"Killed"}, (line, verdicts)


# A SCRIPT ERROR outside every test: an autoload's `_ready` reads a null. Both JUnit frameworks run
# the suite green anyway, so the baseline passes, but the marker run must stop, because an error
# like this can cut short code a test would have reached.
_NOISY = """extends Node


func _ready() -> void:
\tvar missing: Variant = null
\tmissing.size()
"""


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_a_script_error_outside_every_test_stops_the_marker_run(
    tmp_path: Path, runner: str
) -> None:
    if not (ADDON if runner == "gdunit4" else GUT_ADDON).is_dir():
        pytest.skip(f"{runner} addon not installed")
    project = _coverage_project(
        tmp_path, "noisy", {"noisy.gd": _NOISY}, autoloads='Noisy="*res://noisy.gd"'
    )
    args = [*_runner_args(runner), "--coverage-analysis", "all"]
    done, report = _gdmutant(project, TARGET, args, tmp_path / "noisy.json")
    assert done.returncode == 1, done.stdout + done.stderr
    assert report is None
    assert "the coverage marker run was not clean" in done.stderr
    assert "runtime error" in done.stderr
    assert "SCRIPT ERROR" in done.stderr
    assert "--coverage-analysis off" in done.stderr
    # The same project without markers runs to completion: only the marker run scans for it.
    plain, plain_report = _gdmutant(project, TARGET, _runner_args(runner), tmp_path / "plain.json")
    assert plain.returncode == 0, plain.stderr
    assert plain_report is not None


# Per-file test selection (docs/decisions/0017, Plan step 3), against real Godot and both JUnit
# frameworks. The bar is the same two-sided evidence step 2 had, with a harder question behind it: a
# mutant no longer runs every test, so a map that drops the one test file that could kill it would
# turn a kill into a survivor and nothing else in the run would say so. Every test below is either
# that check or one of the ways the ADR says the map can be wrong.

#: Three functions, one per test file below, so every mutant is reached by exactly one file and
#: killed by that file alone. A map that credited any of them to the wrong file would report a
#: survivor, which is what makes the selected runs here worth measuring.
_SPLIT = """class_name Split
extends RefCounted


static func earlier(a: int, b: int) -> bool:
\treturn a > b


static func under(value: int, cap: int) -> bool:
\treturn value < cap


static func tripled(value: int) -> int:
\treturn value * 3
"""
#: Each mutated line of `_SPLIT`, and the function it sits in.
_SPLIT_LINES = {6: "earlier", 10: "under", 14: "tripled"}


def _suite(framework: str, name: str, body: str) -> str:
    """One test suite for `framework`, holding a single test called `name` with `body`."""
    head = "extends GdUnitTestSuite" if framework == "gdunit4" else "extends GutTest"
    return f"{head}\n\n\nfunc test_{name}() -> void:\n{body}\n"


def _split_tests(framework: str) -> dict[str, str]:
    """One suite per function of `_SPLIT`, each in its own file."""
    if framework == "gdunit4":
        bodies = {
            "earlier": "\tassert_bool(Split.earlier(5, 3)).is_true()\n"
            "\tassert_bool(Split.earlier(3, 3)).is_false()",
            "under": "\tassert_bool(Split.under(1, 4)).is_true()\n"
            "\tassert_bool(Split.under(4, 4)).is_false()",
            "tripled": "\tassert_int(Split.tripled(2)).is_equal(6)",
        }
        folder = "split_test"
    else:
        bodies = {
            "earlier": "\tassert_true(Split.earlier(5, 3))\n\tassert_false(Split.earlier(3, 3))",
            "under": "\tassert_true(Split.under(1, 4))\n\tassert_false(Split.under(4, 4))",
            "tripled": "\tassert_eq(Split.tripled(2), 6)",
        }
        folder = "split_gut"
    return {
        f"{folder}/test_{name}.gd": _suite(framework, name, body) for name, body in bodies.items()
    }


def _live_runner(runner: str, tests: str) -> Runner:
    """The shipped runner for `runner`, pointed at the test directory `tests`."""
    if runner == "gdunit4":
        return GdUnit4Runner(test_path=tests, godot=_GODOT_EXE)
    return GutRunner(test_dir=tests, godot=_GODOT_EXE)


def _per_file_run(
    project: Path, runner: str, tests: str, self_check: int | None = 3, target: str = "split.gd"
) -> MutationRun:
    """Drive the engine itself with ``--coverage-analysis per-file``.

    The two-sided check below goes through the CLI, because that is what ships. This is for the
    questions the JSON report cannot answer: how many test files a given mutant actually ran,
    whether its kill was trusted, and how many marked lines the two passes disagreed about.
    """
    source = (project / target).read_text(encoding="utf-8")
    return engine_run(
        str(project),
        str(project / target),
        source,
        _live_runner(runner, tests),
        ADAPTER,
        coverage=CoverageAnalysis.PER_FILE,
        marker=GDScriptMarker(godot=_GODOT_EXE),
        self_check=self_check,
    )


def _skip_without(runner: str) -> None:
    if not (ADDON if runner == "gdunit4" else GUT_ADDON).is_dir():
        pytest.skip(f"{runner} addon not installed")


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_per_file_selection_gives_every_mutant_the_verdict_it_gets_without_it(
    tmp_path: Path, runner: str
) -> None:
    """The ADR's two-sided check, with the self-check on every mutant, through the shipped CLI.

    Not vacuous: each of the three functions is reached and killed by one test file alone, so a map
    that dropped that file would report a survivor while the run still looked perfectly healthy.
    """
    _skip_without(runner)
    tests = _split_tests(runner)
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(tmp_path, f"split-{runner}", {"split.gd": _SPLIT, **tests})
    args = _runner_args(runner, test_dir)
    off, off_report = _gdmutant(project, "split.gd", args, tmp_path / f"{runner}-off.json")
    assert off.returncode == 0, off.stdout + off.stderr
    on, on_report = _gdmutant(
        project,
        "split.gd",
        [*args, "--coverage-analysis", "per-file", "--coverage-self-check", "all"],
        tmp_path / f"{runner}-on.json",
    )
    assert on.returncode == 0, on.stdout + on.stderr
    assert off_report is not None and on_report is not None
    assert not _assert_two_sided(off_report, on_report), "every line here is reached"
    killed = {key for key, status in _statuses(on_report).items() if status == "Killed"}
    assert {line for line, _, _, _ in killed} == set(_SPLIT_LINES)
    assert "mutants ran only the test files that reach them (the suite has 3 test files)" in (
        on.stdout
    )
    assert "mutants that ran only some test files against the whole suite" in on.stdout


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_each_mutant_runs_one_of_the_three_test_files(tmp_path: Path, runner: str) -> None:
    """The saving itself, measured: every mutant ran a third of the suite, not all of it."""
    _skip_without(runner)
    tests = _split_tests(runner)
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(tmp_path, f"share-{runner}", {"split.gd": _SPLIT, **tests})
    result = _per_file_run(project, runner, test_dir, self_check=0)
    assert result.test_files == 3
    ran = [o for o in result.outcomes if o.verdict is not Verdict.INVALID]
    assert ran, "no mutant ran"
    assert {o.selected for o in ran} == {1}
    assert result.order_dependent == 0
    assert result.order_coupled == 0


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_a_line_reached_only_at_load_time_runs_every_test_file(tmp_path: Path, runner: str) -> None:
    """Stryker's static-mutant rule, live: an autoload's ``_init`` runs before any test file opens
    a window, so nothing may be credited with it and every test has to run."""
    _skip_without(runner)
    tests = _split_tests(runner)
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(
        tmp_path,
        f"boot-{runner}",
        {"boot.gd": _BOOT, "split.gd": _SPLIT, **tests},
        autoloads='Boot="*res://boot.gd"',
    )
    result = _per_file_run(project, runner, test_dir, self_check=0, target="boot.gd")
    by_line: dict[int, set[int | None]] = {}
    for outcome in result.outcomes:
        by_line.setdefault(outcome.mutant.span.line, set()).add(outcome.selected)
    # `_compute`, called from `_init`, is reached with no test file running: every test ran for it.
    assert by_line[11] == {None}
    # `_unused` is called by nothing at all, which is a different answer from "runs everything".
    assert {o.verdict for o in result.outcomes if o.mutant.span.line == 15} == {Verdict.NO_COVERAGE}


#: A timer one test file starts and never waits for, whose callback therefore fires while some
#: *other* file is running. Nothing asserts `ticks`, on purpose: this fixture is about which file
#: the hit is credited to, and a test that also checked the value would make the suite depend on
#: the order its files run in, which is a different rule with a different answer.
_CROSSING = """class_name Crossing
extends RefCounted

static var ticks := 0


static func start_timer() -> void:
\tvar tree := Engine.get_main_loop() as SceneTree
\ttree.create_timer(0.6).timeout.connect(_tick)


static func started() -> bool:
\treturn ticks >= 0


static func _tick() -> void:
\tticks = 3
"""
#: The two lines of `_CROSSING` the test below is about, found in the fixture rather than counted
#: by hand, so editing the fixture cannot leave the test asserting about a blank line.
_TICK_LINE = _CROSSING.split("\n").index("\tticks = 3") + 1
_TIMER_LINE = next(
    number for number, line in enumerate(_CROSSING.split("\n"), 1) if "create_timer" in line
)


def _crossing_tests(framework: str) -> dict[str, str]:
    """Two files that each start a timer and then wait for less time than it needs, and one that
    touches nothing.

    Each timer therefore fires after its own file's window has closed, whichever order the
    framework runs them in, and lands either inside the other file's window or after every window.
    Both of those make the line it sets one no single file can be credited with, so this does not
    depend on which file the framework decides to run first. It does not on every platform:
    GdUnit4's discovery order is not alphabetical on Linux.

    The third file exists so that selection has something to leave out, which is what makes the
    assertion about the timer's own line worth making.
    """
    if framework == "gdunit4":
        folder = "cross_test"
        starts = "\tCrossing.start_timer()\n\tawait get_tree().create_timer(0.4).timeout"
        starts += "\n\tassert_bool(Crossing.started()).is_true()"
        idle = "\tassert_bool(true).is_true()"
    else:
        folder = "cross_gut"
        starts = "\tCrossing.start_timer()\n\tawait wait_seconds(0.4)"
        starts += "\n\tassert_true(Crossing.started())"
        idle = "\tassert_true(true)"
    return {
        f"{folder}/test_a_starts.gd": _suite(framework, "starts_a_timer", starts),
        f"{folder}/test_b_starts.gd": _suite(framework, "starts_another_timer", starts),
        f"{folder}/test_c_idle.gd": _suite(framework, "touches_nothing", idle),
    }


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_deferred_code_that_crosses_test_files_runs_every_test_file(
    tmp_path: Path, runner: str
) -> None:
    """A timer's callback belongs to no single test file, and gdmutant must not pretend it does.

    Each of two files starts a timer and then waits for less time than the timer needs, so every
    callback fires after its own file's window has closed: either inside another file's window,
    where the forward and reverse passes cannot agree about which, or after every window, which is
    load-time code. Either way the line it sets runs every test rather than the one file that
    happened to be on screen when it went off.
    """
    _skip_without(runner)
    tests = _crossing_tests(runner)
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(tmp_path, f"cross-{runner}", {"crossing.gd": _CROSSING, **tests})
    result = _per_file_run(project, runner, test_dir, self_check=0, target="crossing.gd")
    by_line: dict[int, set[int | None]] = {}
    verdicts: dict[int, set[Verdict]] = {}
    for outcome in result.outcomes:
        by_line.setdefault(outcome.mutant.span.line, set()).add(outcome.selected)
        verdicts.setdefault(outcome.mutant.span.line, set()).add(outcome.verdict)
    # `ticks = 3`, set from the timer's callback, is the line no single file can be credited with.
    assert by_line[_TICK_LINE] == {None}
    assert Verdict.NO_COVERAGE not in verdicts[_TICK_LINE], (
        "a line a timer reaches is not unreached"
    )
    # Not vacuous: the timer's own line is reached inside the two starters' own windows and is
    # selected to just those two of the three files, so this project does select, and the line the
    # callback sets is specifically the one it will not.
    assert result.test_files == 3
    assert by_line[_TIMER_LINE] == {2}


#: Shared state one suite can seed and another can depend on. `seed` returns its value so a suite
#: can call it from a class-level `static var`, which Godot runs when it *loads* the script rather
#: than when it runs a test.
_SHARED = """class_name Shared
extends RefCounted

static var seeded := 0


static func seed() -> int:
\tseeded = 4
\treturn seeded


static func doubled(value: int) -> int:
\treturn value * 2
"""
#: The line of `_SHARED` only the dependent suite below reaches, found in the fixture rather than
#: counted by hand.
_DOUBLED_LINE = _SHARED.split("\n").index("\treturn value * 2") + 1


def _shared_bodies(framework: str) -> tuple[str, str]:
    """The seeding suite's test body and the dependent suite's, for `framework`."""
    if framework == "gdunit4":
        return (
            "\tassert_int(Shared.seeded).is_equal(4)",
            "\tassert_int(Shared.seeded).is_equal(4)\n\tassert_int(Shared.doubled(3)).is_equal(6)",
        )
    return (
        "\tassert_eq(Shared.seeded, 4)",
        "\tassert_eq(Shared.seeded, 4)\n\tassert_eq(Shared.doubled(3), 6)",
    )


def _coupled_tests(framework: str, folder: str) -> dict[str, str]:
    """A suite that seeds shared state **when its script loads**, and one that needs it.

    Loading is what makes this independent of run order: both frameworks load every suite they were
    given before they run any of them, so the dependent suite passes wherever it lands. Run it on
    its own, though, and the seeding suite is never loaded, so it fails for a reason that has
    nothing to do with any mutant. That is order coupling neither marker pass can see, which is
    what the confirmation of a kill is for.
    """
    seed_body, user_body = _shared_bodies(framework)
    head = "extends GdUnitTestSuite" if framework == "gdunit4" else "extends GutTest"
    seeder = (
        f"{head}\n\nstatic var _seeded := Shared.seed()\n\n\n"
        f"func test_seeded() -> void:\n{seed_body}\n"
    )
    return {
        f"{folder}/test_a_seed.gd": seeder,
        f"{folder}/test_b_user.gd": _suite(framework, "uses", user_body),
    }


def _run_order(project: Path, runner: str, test_dir: str) -> list[str]:
    """The test files of `test_dir`, by file stem, in the order this framework runs them here.

    Asked rather than assumed. GdUnit4's discovery order is alphabetical on Windows and is not on
    Linux, and a fixture that needs one file to run before another has to know which way round it
    will be on the machine it is running on.
    """
    result = _live_runner(runner, test_dir).run(str(project))
    return [Path(suite.name.split(".")[0]).stem for suite in result.suites]


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_a_kill_is_not_believed_when_its_test_files_fail_unmutated(
    tmp_path: Path, runner: str
) -> None:
    """One file reaches `doubled` and nothing else does, so a mutant there runs against that file
    alone, where it fails because the file that seeds its shared state was never loaded. The kill is
    confirmed against the unmutated source, found not to be the mutant's, and the whole suite
    decides instead."""
    _skip_without(runner)
    folder = "coupled_test" if runner == "gdunit4" else "coupled_gut"
    tests = _coupled_tests(runner, folder)
    project = _coverage_project(tmp_path, f"coupled-{runner}", {"shared.gd": _SHARED, **tests})
    result = _per_file_run(project, runner, f"res://{folder}", self_check=0, target="shared.gd")
    doubled = [o for o in result.outcomes if o.mutant.span.line == _DOUBLED_LINE]
    assert doubled, "the fixture no longer has a line only the dependent file reaches"
    assert result.order_coupled >= 1
    coupled = [o for o in doubled if o.order_coupled]
    assert coupled
    assert {o.selected for o in coupled} == {None}  # the whole suite had the last word


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_a_suite_that_depends_on_file_order_refuses_selection_and_keeps_no_coverage(
    tmp_path: Path, runner: str
) -> None:
    """Selection is unsound for such a suite, so gdmutant says so and stops selecting for that run.

    The fixture is built in two steps, because a suite that only passes one way round has to know
    which way round this framework runs it: two seeding suites go in, the framework is asked which
    one it runs last, and that one is rewritten to depend on the other having gone first.

    It is not an error: the forward pass alone is enough for the `no coverage` verdict, which is
    what the run still reports."""
    _skip_without(runner)
    folder = "ordered_test" if runner == "gdunit4" else "ordered_gut"
    seed_body, user_body = _shared_bodies(runner)
    seeder = _suite(runner, "seeds", "\tShared.seed()\n" + seed_body)
    project = _coverage_project(
        tmp_path,
        f"ordered-{runner}",
        {
            "shared.gd": _SHARED,
            f"{folder}/test_a_one.gd": seeder,
            f"{folder}/test_b_two.gd": seeder,
        },
    )
    last = _run_order(project, runner, f"res://{folder}")[-1]
    (project / folder / f"{last}.gd").write_text(
        _suite(runner, "uses", user_body), encoding="utf-8", newline="\n"
    )
    args = [*_runner_args(runner, f"res://{folder}"), "--coverage-analysis", "per-file"]
    done, report = _gdmutant(project, "shared.gd", args, tmp_path / f"ordered-{runner}.json")
    assert done.returncode == 0, done.stdout + done.stderr
    assert report is not None
    assert "depends on the order its files run in" in done.stderr
    assert "will not select tests for this run" in done.stderr
    assert "selected:" not in done.stdout
    assert "Mutation score:" in done.stdout


class _Blindfolded:
    """Wraps the real marker, then makes the recorder credit one test file's hits to another.

    That is the ADR's "a dropped credit", in the one direction that matters. The file still runs,
    still passes and still opens its window, so nothing about the marker run looks wrong: the suite
    is green, the windows are all there, the hits file is written. The only consequence is that the
    mutants that file alone could kill are run against a file that cannot kill them.

    Mis-crediting rather than simply dropping is the point. A dropped hit makes the line look
    unreached, which the "no coverage" half of the self-check already catches. This is the half
    that only step 3 needs: a line that really is reached, run against the wrong tests."""

    def __init__(self, blind_to: str, credit_to: str) -> None:
        self.inner = GDScriptMarker(godot=_GODOT_EXE)
        self.blind_to = blind_to
        self.credit_to = credit_to

    def mark(self, copy_dir: str, files: Any) -> MarkedCopy:
        marks = Path(copy_dir) / RECORDER_DIR / "marks.gd"
        marked = self.inner.mark(copy_dir, files)
        text = marks.read_text(encoding="utf-8")
        broken = text.replace(
            "static func _record(spot: int) -> void:\n\tlast[spot] = window",
            "static func _record(spot: int) -> void:\n"
            "\tvar credited := window\n"
            f'\tif credited.ends_with("{self.blind_to}"):\n'
            f'\t\tcredited = "{self.credit_to}"\n'
            "\tlast[spot] = credited",
        ).replace(
            "\tif not windows.has(window):\n"
            "\t\twindows[window] = {}\n"
            "\twindows[window][spot] = true",
            "\tif not windows.has(credited):\n"
            "\t\twindows[credited] = {}\n"
            "\twindows[credited][spot] = true",
        )
        assert "credited" in broken, "the recorder no longer has the shape this sabotage edits"
        assert "windows[window][spot]" not in broken
        marks.write_text(broken, encoding="utf-8", newline="\n")
        return marked


@pytest.mark.parametrize("runner", ["gdunit4", "gut"])
def test_a_map_that_drops_the_file_that_kills_a_mutant_is_caught_loudly(
    tmp_path: Path, runner: str
) -> None:
    """The failure this whole step has to survive, built on purpose.

    Nothing else in the run notices: the suite is green, every file opens its window, the hits file
    is there, and the mutant simply reads as survived. Only the self-check, which runs it against
    the whole suite as well, can tell."""
    _skip_without(runner)
    tests = _split_tests(runner)
    test_dir = "res://" + next(iter(tests)).split("/")[0]
    project = _coverage_project(tmp_path, f"blind-{runner}", {"split.gd": _SPLIT, **tests})
    with pytest.raises(CoverageSelfCheckFailed) as caught:
        engine_run(
            str(project),
            str(project / "split.gd"),
            _SPLIT,
            _live_runner(runner, test_dir),
            ADAPTER,
            coverage=CoverageAnalysis.PER_FILE,
            marker=_Blindfolded("test_earlier.gd", f"{test_dir}/test_under.gd"),
            self_check=None,
        )
    assert "gave 'survived'" in str(caught.value)
    assert "running it against the whole suite gave 'killed'" in str(caught.value)
    # And the project was left exactly as it was.
    assert (project / "split.gd").read_text(encoding="utf-8") == _SPLIT
