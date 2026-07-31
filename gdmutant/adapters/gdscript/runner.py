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

from gdmutant.engine.runner import SuiteResult, SuiteTimeout, parse_junit_xml, with_filename

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
            try:
                subprocess.run(
                    [
                        self.godot,
                        "--headless",
                        "--path",
                        str(Path(project_dir).resolve()),
                        "--import",
                    ],
                    cwd=project_dir,
                    timeout=_IMPORT_TIMEOUT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError as error:
                raise with_filename(error, self.godot) from error
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
        # Ensure the report's parent directory exists. GUT (unlike GdUnit4, which creates its own
        # reports/report_N/) will NOT create the directory for -gjunit_xml_file: on a fresh project
        # with no reports/ dir it runs the whole suite green but then fails to export with "Could
        # not create export file", writing no report — so every run would raise "wrote no report".
        # Harmless for GdUnit4 (it writes into this pre-made dir exactly as it did when it made it).
        report.parent.mkdir(parents=True, exist_ok=True)
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
        except FileNotFoundError as error:
            raise with_filename(error, self.godot) from error
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

    **Crash-safety (`engine.runner.Runner`) — the GUT-specific hardening.** When a test file fails
    to *compile/load*, GUT does **not** fail the run: it **skips** that suite, runs the remaining
    ones, and exits 0 (confirmed live against GUT v9.7.1 by the n>1 probe). So a mutant that
    breaks only the file(s) referencing the mutated source yields a report of the *healthy* suites'
    green tests → a pass → SURVIVED: a **false survivor**, gdmutant's worst failure. `tests == 0`
    (the whole run zeroed — GUT's empty-report shape, or every suite skipped) does not catch this,
    because the healthy suites still ran. So `_result_from_report` upholds the clause two ways —
    both **errors** — with a third, symmetric **warning** closing the loop:
      1. **`tests == 0` → error** — the empty-report / all-skipped shape (raise → the engine tallies
         ``error``), never a zero-test pass.
      2. **a drop below the baseline test count → error** — the first run (the engine's healthy
         baseline) fixes the expected test count; any later run with *fewer* tests is surfaced as
         ``error`` rather than a false survivor. This is the widening the probe proved necessary
         (GUT skips-and-continues). **It assumes deterministic, stable suite collection** (as all
         mutation testing does); under that assumption any skipped suite strictly drops the scalar
         total → error, never a silent pass. It does **not** cover a suite whose test count varies
         run-to-run: such variance can *mask* a real skip (if another suite rises by the same
         amount — a residual false survivor the scalar total can't see) or *false-error* on a benign
         dip. That residual variance is exactly what the canary (3) makes observable.
      3. **`tests > baseline` → run-level WARNING (never an error).** A legitimate mutant can never
         raise the collected test count *above* the healthy baseline — a mutation cannot add test
         files — so a later run reporting MORE tests than the baseline deterministically proves the
         baseline *undercounted*: suite collection is non-deterministic, the one condition (2)'s
         stability assumption excludes. This is the **canary** that makes the otherwise-unobservable
         variance-masking case observable (a silent false survivor can never be *seen*, so anchoring
         the widening on "variance observed in practice" was itself unobservable — this closes that
         gap). It is surfaced as a **warning** via `run_warning` (on the same stderr surface as the
         "all mutants survived" warning), **never** an error — flipping the mutant to error would
         false-error on benign flakiness. **When it fires, that is the trigger to widen to per-suite
         baseline tracking** (the correctly-deferred work); until then the scalar-total guard
         stands. Stabilize the flaky suite it names first.

    Thread-safety under ``--jobs``: the baseline floor is set on the first run (the engine runs the
    baseline serially, *before* it fans mutants out to workers) and only **read** thereafter, so the
    one shared instance is safe across worker threads (they never write it). The canary flag is only
    ever set to ``True`` (idempotent, single-valued), so concurrent worker writes are safe too.
    """

    test_dir: str = "res://test"
    report_path: str = DEFAULT_GUT_REPORT_PATH
    godot: str = "godot"
    timeout: float = DEFAULT_TIMEOUT
    _imported: bool = field(default=False, init=False, repr=False)
    #: The healthy baseline's test count, captured on the first run; a later run with fewer tests is
    #: a skipped (failed-to-load) suite → error. ``None`` until the first run establishes it.
    _baseline_tests: int | None = field(default=None, init=False, repr=False)
    #: Non-determinism canary: set once any run collects MORE tests than the baseline (which a
    #: legitimate mutant cannot cause), proving collection is non-deterministic. Read out by
    #: `run_warning` as a run-level warning; never raises. See the class docstring, point (3).
    _nondeterminism_canary: bool = field(default=False, init=False, repr=False)
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
        """Crash-safety (see the class docstring): raise — never return a pass — when the report
        reflects a suite that failed to load rather than a real, complete run.

        Two shapes, both surfaced as an execution error:
          * **zero tests** — GUT's empty-report shape (a ``<testsuites tests="0"/>`` with no child,
            which the parser raises ``ValueError`` on — caught here — or a child ``<testsuite
            tests="0">``), or every suite skipped;
          * **fewer tests than the baseline** — GUT skips a suite whose source-under-test won't
            compile and runs the rest green, so a drop below the first (healthy baseline) run's test
            count means a suite was skipped: the false-survivor case zero-test alone misses.

        **Zero tests on the BASELINE run is a different fault and gets a different message.** The
        baseline runs the *unmutated* source, so nothing gdmutant did can have broken it — a suite
        that compiles fine cannot have been skipped by a mutant that does not exist yet. The
        overwhelmingly likely cause is discovery: ``--tests`` defaults to ``res://test`` while GUT's
        own documented layout puts suites in ``test/unit/``, and GUT's ``-gdir`` does **not**
        recurse. Confirmed live (GUT v9.7.1, Godot 4.7): a stock GUT project collects
        zero tests under the default and GUT prints "Nothing was run." Reporting a compile/load
        failure there sends the user to debug a crash that isn't happening, and never names the flag
        that fixes it.
        """
        try:
            result = parse_junit_xml(report_text)
        except ValueError:
            result = None  # no <testsuite> at all — GUT's empty crash report
        tests = result.tests if result is not None else 0
        baseline = self._baseline_tests
        is_baseline = baseline is None
        if baseline is None:
            # First run = the engine's healthy baseline (run serially before any --jobs fan-out): it
            # fixes the expected count. Later runs only read it, so the shared instance is safe.
            self._baseline_tests = tests
            baseline = tests
        elif tests > baseline:
            # Non-determinism canary (symmetric to the < baseline guard below). A legitimate mutant
            # can never raise the collected test count ABOVE the baseline — a mutation cannot add
            # test files — so more tests than the healthy baseline deterministically proves the
            # baseline undercounted: suite collection is non-deterministic. That degrades the
            # < baseline guard (a real skip can be masked by a flaky suite rising to compensate).
            # Flag it (read out by `run_warning`); NEVER raise — benign flakiness must not
            # false-error the mutant. Setting a bool from --jobs workers is safe (only ever True).
            self._nondeterminism_canary = True
        if result is None or tests == 0 or tests < baseline:
            detail = (completed.stderr or completed.stdout or "").strip()
            if is_baseline:
                # Discovery, not a crash — see the docstring. Mid-run drops keep the message below.
                message = (
                    f"GUT found no tests under {self.test_dir} on the unmutated (baseline) run, so "
                    "this is test discovery, not a broken suite — no mutant existed yet. GUT's "
                    "-gdir does not search subdirectories, and GUT's own layout puts suites in "
                    "test/unit/: point gdmutant at them with --tests res://test/unit (or wherever "
                    "yours live). One directory only — for a tree of suites, run GUT yourself with "
                    "-ginclude_subdirs via --runner command"
                )
            else:
                reason = (
                    "GUT ran 0 tests"
                    if tests == 0
                    else f"GUT ran {tests} tests, fewer than the baseline {baseline}"
                )
                message = (
                    f"{reason} — a test suite failed to compile/load and GUT skipped it (it runs "
                    "the rest green and exits 0, so this would otherwise be a false survivor)"
                )
            raise RuntimeError(message + (f":\n{detail[-1000:]}" if detail else ""))
        return result

    def run_warning(self) -> str | None:
        """The non-determinism canary as a run-level warning (`engine.runner.RunWarning`), or
        ``None`` when it never fired. See the class docstring's crash-safety point (3): a run
        collected MORE tests than the healthy baseline — which a legitimate mutant cannot cause — so
        test collection is non-deterministic and the crash-safety drop-guard's protection against a
        silently-masked skipped suite is degraded here. A warning, never an error: it leaves the
        mutation score and exit code unchanged."""
        if not self._nondeterminism_canary:
            return None
        return (
            "warning: test collection was non-deterministic — a run collected more tests than the "
            "healthy baseline, which a legitimate mutant cannot cause (a mutation cannot add test "
            "files). The crash-safety guard's protection against a silently-masked skipped test "
            "suite is degraded in this environment; investigate flaky suite loading. This is the "
            "trigger to build per-suite baseline tracking. The mutation score and exit code are "
            "unchanged."
        )
