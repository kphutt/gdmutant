"""The Godot/GdUnit4 test runner — the live half of the GDScript adapter.

Shells out to ``godot --headless`` running GdUnit4's command-line tool, then parses the JUnit
report it writes (via `engine.runner.parse_junit_xml`). GdUnit4 returns a non-zero exit code when
tests fail, so the exit code is ignored — the report is the source of truth.

The exact GdUnit4 CLI flags and report location are validated **live in CI** (they need real Godot
+ the GdUnit4 addon). Unit tests here cover command construction and report parsing with the
subprocess mocked.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from gdmutant.engine.runner import SuiteResult, SuiteTimeout, parse_junit_xml

_GDUNIT_CMD_TOOL = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"

# The runner's defaults, exposed so the CLI can present them (and its --report-path/--timeout
# defaults) from one source, without reading them off the class at parse time (which breaks when a
# test monkeypatches GdUnit4Runner).
DEFAULT_REPORT_PATH = "reports/report_1/results.xml"
DEFAULT_TIMEOUT = 600.0

# The one-time import warm-up gets its own generous budget, independent of the per-suite timeout: on
# a cold checkout Godot imports every asset, which can take much longer than a single test run.
_IMPORT_TIMEOUT = 300.0


@dataclass
class GdUnit4Runner:
    """Runs a project's GdUnit4 suite headlessly and parses the JUnit report.

    `test_path` is the GdUnit4 test directory (a ``res://`` path). `report_path` is where GdUnit4
    writes its JUnit XML, relative to the project dir. `godot` is the Godot executable.
    """

    test_path: str = "res://test"
    report_path: str = DEFAULT_REPORT_PATH
    godot: str = "godot"
    timeout: float = DEFAULT_TIMEOUT
    _imported: bool = field(default=False, init=False, repr=False)

    def prepare(self, project_dir: str) -> None:
        """Warm Godot's import cache once, so ``class_name`` types resolve on a cold checkout.

        The engine's `Preparable` hook (called once before it times the baseline, so this scan's
        cost never inflates the derived per-mutant timeout or the ETA); ``run`` also
        calls it defensively, so a direct ``run`` still works cold. Idempotent via ``_imported``.

        GdUnit4's ``GdUnitCmdTool.gd`` references ``class_name`` types (``GdUnitTestCIRunner``, …)
        that only resolve after Godot writes ``.godot/global_script_class_cache.cfg`` — which only
        the ``--import`` scan does. Without this, the *baseline* suite fails to even load the tool
        on a fresh clone ("Could not find type … in the current scope"), so a first-time adopter
        can't run at all. Warm it once: the cache persists across mutants (mutating a method body
        never changes class registration), so re-importing per mutant would just burn a Godot boot.

        The exit code is ignored — ``--import`` returns non-zero on benign addon/import chatter
        across Godot versions — and any failure is left to surface as the usual "wrote no report"
        error from the real run, rather than masking it behind a warm-up error.
        """
        if self._imported:
            return
        # A pathologically slow import shouldn't itself abort the run; suppress its timeout and let
        # the real suite run (with its own timeout) surface a genuine problem as "wrote no report".
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                [self.godot, "--headless", "--path", str(Path(project_dir).resolve()), "--import"],
                cwd=project_dir,
                timeout=_IMPORT_TIMEOUT,
                check=False,
                capture_output=True,
                text=True,
            )
        # Mark done only once the scan has completed — or a slow import was deliberately given up
        # on (a suppressed timeout falls through to here). A *non-timeout* failure (a transient
        # OSError, a permission error, Godot crashing) propagates out before this, leaving the
        # warm-up retryable on a reused runner instance rather than silently skipped forever after
        # The shipped CLI builds a fresh runner per run, but a library/daemon
        # reuse would otherwise poison retry.
        self._imported = True

    def command(self, project_dir: str) -> list[str]:
        """The ``godot --headless`` command that runs the GdUnit4 suite for `project_dir`.

        ``-rc 1`` (report-count = 1) is essential: GdUnit4's CI runner otherwise keeps a report
        history, writing each invocation to an incrementing ``reports/report_N/`` dir. Since the
        engine calls this once per mutant against the same project, re-reading a fixed
        ``report_path`` would then return the *baseline's* stale report for every mutant — silently
        marking every mutant SURVIVED. ``-rc 1`` forces overwrite-in-place so `report_path` is
        always the latest run.

        ``--ignoreHeadlessMode`` is required: modern GdUnit4 (verified live against v6.1.3) aborts
        under ``--headless`` with exit 103 and writes *no report* unless this flag is passed —
        without it, the runner would raise on every invocation (see the live self-test that caught
        this). Mutation testing is inherently a headless/CI activity over logic tests, so GdUnit4's
        UI-interaction guard never applies here; ignoring it is always correct.
        """
        # Resolve --path to an absolute path: run() sets ``cwd=project_dir``, so a *relative*
        # project_dir (e.g. ``--project corpus``) would otherwise be applied twice — Godot would
        # look for ``corpus/corpus`` and abort with "Invalid project path" (caught by the live
        # self-test). An absolute --path is cwd-independent; absolute inputs are unchanged.
        return [
            self.godot,
            "--headless",
            "--path",
            str(Path(project_dir).resolve()),
            "-s",
            _GDUNIT_CMD_TOOL,
            "-a",
            self.test_path,
            "-rc",
            "1",
            "--ignoreHeadlessMode",
        ]

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        # Warm Godot's import cache once (before the very first suite run) so GdUnit4's class_name
        # types resolve on a cold checkout; a no-op if the engine already prepared, and on
        # every subsequent mutant.
        self.prepare(project_dir)
        budget = self.timeout if timeout is None else timeout
        report = Path(project_dir) / self.report_path
        # Read THIS run's report, never a stale one from a previous mutant: remove it first and
        # require it to reappear. If GdUnit4/Godot writes no report (a crash, an addon-load
        # failure, or a mutant that errors at load time), that's an execution failure — raise so
        # the loop tallies it as ERROR rather than silently inheriting the old verdict (NF-5).
        report.unlink(missing_ok=True)
        # check=False: GdUnit4 exits non-zero on test failures (expected) — the report decides.
        # capture_output: keep per-mutant Godot/GdUnit4 chatter off the console, and retain it so a
        # failed run can be diagnosed instead of vanishing.
        try:
            completed = subprocess.run(
                self.command(project_dir),
                cwd=project_dir,
                timeout=budget,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as expired:
            # A mutation that makes the suite hang is a detection — surface it as a timeout so the
            # engine tallies Timeout (killed), not a no-report error.
            raise SuiteTimeout(f"GdUnit4 run exceeded {budget:g}s") from expired
        if not report.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"GdUnit4 wrote no report at {report} — Godot may have failed to run"
                + (f":\n{detail[-1000:]}" if detail else "")
            )
        return parse_junit_xml(report.read_text(encoding="utf-8"))
