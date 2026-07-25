"""The Godot JUnit test runners — the live half of the GDScript adapter.

gdmutant treats **GdUnit4 and GUT as peer adapters over one shared contract** (the engine's `Runner`
protocol, `engine.runner`): both shell out to ``godot --headless`` running the framework's
command-line tool, then parse the JUnit report it writes (via `engine.runner.parse_junit_xml`, which
is framework-neutral). Neither is privileged in the engine — the engine only ever sees a `Runner`.
The shared machinery (the import warm-up, the report-freshness guard, timeout handling, JUnit
parsing) lives in `_GodotJUnitRunner`; each concrete adapter supplies only its own command flags and
its own **crash-safety** enforcement (see the class docstrings and `engine.runner.Runner`).

For a framework that emits no JUnit XML, the generic exit-code `CommandRunner` (ADR-0005) is the
documented fallback — so the seam is *two* first-class JUnit adapters plus one universal exit-code
path, and any future JUnit-emitting framework becomes first-class by adding one small adapter here,
with no engine change (docs/decisions/0011).

The exact CLI flags and report locations are validated **live in CI** against real Godot + each
addon (they can't be validated with the addon mocked). Unit tests here cover command construction
and report parsing with the subprocess mocked.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from gdmutant.engine.runner import SuiteResult, SuiteTimeout, parse_junit_xml

_GDUNIT_CMD_TOOL = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"
_GUT_CMD_TOOL = "res://addons/gut/gut_cmdln.gd"

# The runners' defaults, exposed so the CLI can present them (and its --report-path/--timeout
# defaults) from one source, without reading them off a class at parse time (which breaks when a
# test monkeypatches a runner).
DEFAULT_REPORT_PATH = "reports/report_1/results.xml"  # GdUnit4's CI-runner report layout
DEFAULT_GUT_REPORT_PATH = "reports/gut_results.xml"  # GUT's -gjunit_xml_file target
DEFAULT_TIMEOUT = 600.0

# The one-time import warm-up gets its own generous budget, independent of the per-suite timeout: on
# a cold checkout Godot imports every asset, which can take much longer than a single test run.
_IMPORT_TIMEOUT = 300.0


@dataclass
class _GodotJUnitRunner:
    """Shared base for the two first-class JUnit adapters (GdUnit4, GUT) — the machinery both need,
    behind the engine's `Runner` (+ `Preparable`) contract. It is **not** a dataclass and is never
    instantiated directly; each concrete adapter is its own dataclass declaring its fields
    (`godot`, `report_path`, `timeout`, an ``_imported`` latch) and setting the ``_framework``
    label.

    What the base owns (identical for both frameworks, so it lives once):
      * `prepare` — the cold-load ``--import`` warm-up so ``class_name`` types resolve on a fresh
        checkout (both frameworks fail to load their command-line tool without it);
      * `run` — the report-freshness guard (remove the old report, require this run's to reappear),
        timeout → `SuiteTimeout`, and JUnit parsing.

    What each adapter supplies:
      * `command` — its own ``godot --headless`` invocation (different flags per framework);
      * `_result_from_report` — its **crash-safety** enforcement (`engine.runner.Runner`): the
        property that a load/compile crash surfaces as a kill or error, never a silent zero-test
        pass. The base default just parses (GdUnit4 relies on the report-reappear guard); GUT
        overrides it to reject a zero-test report.
    """

    # Attributes each concrete adapter dataclass provides. Declared here (no value) so the base's
    # methods type-check; @dataclass on a subclass ignores these (the base is not a dataclass) and
    # reads the subclass's own field declarations, so field order/defaults stay per-adapter.
    godot: str
    report_path: str
    timeout: float
    _imported: bool
    #: Human name of the framework, for error messages ("<name> wrote no report", …).
    _framework: ClassVar[str] = ""

    def prepare(self, project_dir: str) -> None:
        """Warm Godot's import cache once, so ``class_name`` types resolve on a cold checkout.

        The engine's `Preparable` hook (called once before it times the baseline, so this scan's
        cost never inflates the derived per-mutant timeout or the ETA); ``run`` also calls it
        defensively, so a direct ``run`` still works cold. Idempotent via ``_imported``.

        Both GdUnit4's ``GdUnitCmdTool.gd`` and GUT's ``gut_cmdln.gd`` reference ``class_name``
        types that only resolve after Godot writes ``.godot/global_script_class_cache.cfg`` — which
        only the ``--import`` scan does. Without this, the *baseline* suite fails to even load the
        tool on a fresh clone (GdUnit4: "Could not find type … in the current scope"; GUT: "Some GUT
        class_names have not been imported"), so a first-time adopter can't run. Warm it once:
        the cache persists across mutants (mutating a method body never changes class registration),
        so re-importing per mutant would just burn a Godot boot.

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
        # warm-up retryable on a reused runner instance rather than silently skipped forever after.
        # The shipped CLI builds a fresh runner per run, but a library/daemon reuse would otherwise
        # poison retry.
        self._imported = True

    def command(self, project_dir: str) -> list[str]:  # pragma: no cover - overridden per adapter
        """The ``godot --headless`` command that runs this framework's suite for `project_dir`."""
        raise NotImplementedError

    def _result_from_report(
        self, report_text: str, completed: subprocess.CompletedProcess[str]
    ) -> SuiteResult:
        """Parse this run's report into a `SuiteResult`, enforcing the adapter's **crash-safety**
        contract (`engine.runner.Runner`) as it does. The base default just parses (GdUnit4 upholds
        the contract via the report-reappear guard in `run`, so a crash never reaches here). GUT
        overrides this to make a zero-test report an explicit error."""
        return parse_junit_xml(report_text)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        # Warm Godot's import cache once (before the very first suite run) so the framework's
        # class_name types resolve on a cold checkout; a no-op if the engine already prepared — and
        # on every subsequent mutant.
        self.prepare(project_dir)
        budget = self.timeout if timeout is None else timeout
        report = Path(project_dir) / self.report_path
        # Read THIS run's report, never a stale one from a previous mutant: remove it first and
        # require it to reappear. If the framework/Godot writes no report (a crash, an addon-load
        # failure, or a mutant that errors at load time), that's an execution failure — raise so the
        # loop tallies it as ERROR rather than silently inheriting the old verdict (NF-5).
        report.unlink(missing_ok=True)
        # check=False: both frameworks exit non-zero on test failures (expected) — the report
        # decides. capture_output: keep per-mutant Godot chatter off the console, and retain it so a
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
            raise SuiteTimeout(f"{self._framework} run exceeded {budget:g}s") from expired
        if not report.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"{self._framework} wrote no report at {report} — Godot may have failed to run"
                + (f":\n{detail[-1000:]}" if detail else "")
            )
        # Parse under the adapter's crash-safety contract: GdUnit4 just parses; GUT rejects a
        # zero-test report (its empty-report-on-compile-crash form) rather than returning a pass.
        return self._result_from_report(report.read_text(encoding="utf-8"), completed)


@dataclass
class GdUnit4Runner(_GodotJUnitRunner):
    """Runs a project's GdUnit4 suite headlessly and parses the JUnit report.

    `test_path` is the GdUnit4 test directory (a ``res://`` path). `report_path` is where GdUnit4
    writes its JUnit XML, relative to the project dir. `godot` is the Godot executable.

    Crash-safety (`engine.runner.Runner`): GdUnit4 writes *no* report when a test file fails to
    load, which the base's report-reappear guard raises on — so the base's plain
    ``_result_from_report`` (which just parses) suffices; no override is needed.
    """

    test_path: str = "res://test"
    report_path: str = DEFAULT_REPORT_PATH
    godot: str = "godot"
    timeout: float = DEFAULT_TIMEOUT
    _imported: bool = field(default=False, init=False, repr=False)
    _framework: ClassVar[str] = "GdUnit4"

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


@dataclass
class GutRunner(_GodotJUnitRunner):
    """Runs a project's GUT (Godot Unit Test) suite headlessly and parses the JUnit report.

    `test_dir` is the GUT test directory (a ``res://`` path, passed as ``-gdir``). `report_path` is
    where GUT writes its JUnit XML, relative to the project dir (passed as ``-gjunit_xml_file``).
    `godot` is the Godot executable.

    Simpler than GdUnit4 in two ways (validated live against GUT v9.7.1 + Godot 4.7): GUT overwrites
    its report in place (no report-history hazard, so no ``-rc 1`` equivalent is needed), and it
    honours ``--headless`` directly (no ``--ignoreHeadlessMode``).

    **Crash-safety (`engine.runner.Runner`) — the one GUT-specific hardening.** When a test file
    fails to *compile*, GUT writes an **empty** ``<testsuites tests="0" …/>`` and exits **0**.
    Left alone that either parses to a clean zero-test pass (marking the responsible mutant SURVIVED
    — a false survivor, gdmutant's worst failure) or, for the no-child empty form, raises an
    *incidental* ``ValueError`` deep in the parser. `_result_from_report` makes it **explicit**:
    ``tests == 0`` (by any report shape) is an execution **error** (raise → the engine tallies
    ``error``), never a pass.
    """

    test_dir: str = "res://test"
    report_path: str = DEFAULT_GUT_REPORT_PATH
    godot: str = "godot"
    timeout: float = DEFAULT_TIMEOUT
    _imported: bool = field(default=False, init=False, repr=False)
    _framework: ClassVar[str] = "GUT"

    def command(self, project_dir: str) -> list[str]:
        """The ``godot --headless`` command that runs the GUT suite for `project_dir`.

        GUT's command-line flags are ``=``-joined (``-gdir=…``), not space-separated. ``-gexit``
        makes GUT quit when the run finishes (headless CI mode); ``-gjunit_xml_file`` writes the
        JUnit report the engine reads. The report is overwritten in place each run, so — unlike
        GdUnit4 — there is no report-history flag to force.
        """
        # Resolve --path to an absolute path for the same reason as GdUnit4 (run() sets
        # cwd=project_dir; a relative --path would be applied twice).
        return [
            self.godot,
            "--headless",
            "--path",
            str(Path(project_dir).resolve()),
            "-s",
            _GUT_CMD_TOOL,
            f"-gdir={self.test_dir}",
            f"-gjunit_xml_file=res://{self.report_path}",
            "-gexit",
        ]

    def _result_from_report(
        self, report_text: str, completed: subprocess.CompletedProcess[str]
    ) -> SuiteResult:
        """Crash-safety: a GUT report with **zero tests** means a test file failed to compile (GUT
        writes an empty report and exits 0) — surface it as an execution error, never a silent
        zero-test pass that would mark the mutant SURVIVED. Handles both empty-report shapes: a
        ``<testsuites tests="0"/>`` with no child (parser raises ``ValueError`` — caught here), and
        a child ``<testsuite tests="0">`` (parses to ``tests == 0``)."""
        try:
            result = parse_junit_xml(report_text)
        except ValueError:
            result = None  # no <testsuite> at all — GUT's empty crash report
        if result is None or result.tests == 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                "GUT ran 0 tests — a test file likely failed to compile (GUT writes an empty "
                "report and exits 0 in that case)" + (f":\n{detail[-1000:]}" if detail else "")
            )
        return result
