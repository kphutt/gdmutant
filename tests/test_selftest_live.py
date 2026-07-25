"""Live self-test — drive the REAL gdmutant CLI against a REAL Godot binary + the corpus.

This is the end-to-end sanity check that closes the "never run against real Godot" gate:
both runner paths are exercised through the *shipped* CLI (argparse / exit codes / Stryker JSON —
via subprocess, never in-process) and pinned to *exact* per-mutant outcomes, not just "it ran".

It is **env-gated** on ``GDMUTANT_GODOT`` (the path to a godot executable), so a plain
``uv run pytest`` — local dev and the ``verify`` CI job — auto-skips it with zero config. Run it
with, e.g.::

    GDMUTANT_GODOT=godot uv run pytest tests/test_selftest_live.py -v

The CommandRunner test needs only Godot. The GdUnit4 test additionally skips if the addon is not
installed (run ``scripts/install-gdunit4.sh`` first).

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
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

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
        pytest.skip("GdUnit4 addon not installed — run scripts/install-gdunit4.sh")
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
        pytest.skip("GUT addon not installed — run scripts/install-gut.sh")
    project = _corpus_copy(tmp_path)
    report = _run_gdmutant(
        project,
        ["--runner", "gut", "--tests", "res://gut_test", "--godot", str(_GODOT)],
        tmp_path / "gut_report.json",
    )
    _assert_pinned_outcomes(report)


# An uncompilable target (a parse gdtoolkit would reject too, but here it's the file *under test*,
# not a mutant): keeps `class_name TurnOrder` so the TurnOrder-referencing GUT suite still resolves
# the name yet fails to load, while the independent suite stays healthy.
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
        pytest.skip("GUT addon not installed — run scripts/install-gut.sh")
    from gdmutant.adapters.gdscript.runner import GutRunner
    from gdmutant.engine.runner import SuiteResult

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
