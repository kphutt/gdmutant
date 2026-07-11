"""The Godot/GdUnit4 test runner — the live half of the GDScript adapter.

Shells out to ``godot --headless`` running GdUnit4's command-line tool, then parses the JUnit
report it writes (via `engine.runner.parse_junit_xml`). GdUnit4 returns a non-zero exit code when
tests fail, so the exit code is ignored — the report is the source of truth.

The exact GdUnit4 CLI flags and report location are validated **live in CI** (they need real Godot
+ the GdUnit4 addon); see the tracker. Unit tests here cover command construction and report
parsing with the subprocess mocked.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from gdmutant.engine.runner import SuiteResult, parse_junit_xml

_GDUNIT_CMD_TOOL = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"


@dataclass
class GdUnit4Runner:
    """Runs a project's GdUnit4 suite headlessly and parses the JUnit report.

    `test_path` is the GdUnit4 test directory (a ``res://`` path). `report_path` is where GdUnit4
    writes its JUnit XML, relative to the project dir. `godot` is the Godot executable.
    """

    test_path: str = "res://test"
    report_path: str = "reports/report_1/results.xml"
    godot: str = "godot"
    timeout: float = 600.0

    def command(self, project_dir: str) -> list[str]:
        """The ``godot --headless`` command that runs the GdUnit4 suite for `project_dir`."""
        return [
            self.godot,
            "--headless",
            "--path",
            project_dir,
            "-s",
            _GDUNIT_CMD_TOOL,
            "-a",
            self.test_path,
        ]

    def run(self, project_dir: str) -> SuiteResult:
        # check=False: GdUnit4 exits non-zero on test failures (expected) — the report decides.
        subprocess.run(
            self.command(project_dir), cwd=project_dir, timeout=self.timeout, check=False
        )
        report = Path(project_dir) / self.report_path
        return parse_junit_xml(report.read_text(encoding="utf-8"))
