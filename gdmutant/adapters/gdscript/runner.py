"""The Godot/GdUnit4 test runner — the live half of the GDScript adapter.

Shells out to ``godot --headless`` running GdUnit4's command-line tool, then parses the JUnit
report it writes (via `engine.runner.parse_junit_xml`). GdUnit4 returns a non-zero exit code when
tests fail, so the exit code is ignored — the report is the source of truth.

The exact GdUnit4 CLI flags and report location are validated **live in CI** (they need real Godot
+ the GdUnit4 addon); see ROADMAP.md. Unit tests here cover command construction and report
parsing with the subprocess mocked.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from gdmutant.engine.runner import SuiteResult, SuiteTimeout, parse_junit_xml

_GDUNIT_CMD_TOOL = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"

# The runner's defaults, exposed so the CLI can present them (and its --report-path/--timeout
# defaults) from one source, without reading them off the class at parse time (which breaks when a
# test monkeypatches GdUnit4Runner).
DEFAULT_REPORT_PATH = "reports/report_1/results.xml"
DEFAULT_TIMEOUT = 600.0


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

    def run(self, project_dir: str) -> SuiteResult:
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
                timeout=self.timeout,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as timeout:
            # A mutation that makes the suite hang is a detection — surface it as a timeout so the
            # engine tallies Timeout (killed), not a no-report error.
            raise SuiteTimeout(f"GdUnit4 run exceeded {self.timeout:g}s") from timeout
        if not report.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"GdUnit4 wrote no report at {report} — Godot may have failed to run"
                + (f":\n{detail[-1000:]}" if detail else "")
            )
        return parse_junit_xml(report.read_text(encoding="utf-8"))
