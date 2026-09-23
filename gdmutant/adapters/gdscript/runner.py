"""The Godot JUnit test runners — the live half of the GDScript adapter.

gdmutant treats **GdUnit4 and GUT as peer adapters over one shared contract** (the engine's `Runner`
protocol, `engine.runner`): both shell out to ``godot --headless`` running the framework's
command-line tool, then parse the JUnit report it writes (via `engine.runner.parse_junit_xml`, which
is framework-neutral). Neither is privileged in the engine — the engine only ever sees a `Runner`.
The shared machinery (the import warm-up, the report-freshness guard, timeout handling, JUnit
parsing) lives in `_GodotJUnitRunner`; each concrete adapter supplies only its own command flags and
its own **crash-safety** enforcement (see the class docstrings and `engine.runner.Runner`). Each
framework fails differently, so each adapter's enforcement is shaped to *its* failure — GdUnit4
aborts the whole run at discovery, GUT skips the broken suite and runs the rest green — and both are
pinned by a live n>1 probe rather than assumed (`tests/test_selftest_live.py`).

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
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from gdmutant.adapters.gdscript.marker_run import WINDOW_HOOK_NAME
from gdmutant.adapters.gdscript.markers import MARKER_AUTOLOAD
from gdmutant.engine.loop import SourceOutsideProject
from gdmutant.engine.runner import (
    ReportedSuite,
    SuiteResult,
    SuiteTimeout,
    parse_junit_xml,
    script_error_excerpt,
    with_filename,
)

_GDUNIT_CMD_TOOL = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"
_GUT_CMD_TOOL = "res://addons/gut/gut_cmdln.gd"

#: GUT's window hook is a `GutHookScript`, which is not a node, so the recorder's writer autoload
#: cannot load it the way it loads GdUnit4's (`marker_run.WINDOW_HOOK_NAME`, imported above). GUT
#: runs it itself, named with ``-gpre_run_script``, so it gets a name of its own.
_GUT_WINDOW_HOOK_NAME = "gut_windows.gd"

#: What GdUnit4 prints, and then exits 0 writing no report, when discovery finds no test suites
#: under ``-a`` — a wrong ``--tests`` path, or a directory holding no suites (verified live against
#: GdUnit4 v6.1.3 + Godot 4.7, both for a real directory with no suites and for one that does not
#: exist). Matching on it is what lets `GdUnit4Runner` tell "you pointed me at the wrong directory"
#: apart from "Godot crashed", which the bare absence of a report cannot distinguish. Matching is
#: **fail-safe**: if a future GdUnit4 reworded this, the hint simply stops appearing and the generic
#: no-report error is raised as before — the run still fails, it is only diagnosed less precisely.
_GDUNIT_NO_TESTS_MARKER = "No test cases found"

#: What every GDScript file ends in.
_GDSCRIPT_SUFFIX = ".gd"
#: What separates a test file's path from the inner class inside it in a report name. Matched from
#: the right, and with the trailing dot, so a *directory* whose own name holds those letters
#: (``v1.gd_legacy/``) cannot be mistaken for the end of the file's path.
_INNER_CLASS_SEPARATOR = f"{_GDSCRIPT_SUFFIX}."


def _tests_per_file(suites: Sequence[ReportedSuite]) -> dict[str, int]:
    """How many tests each test *file* has, from GUT's report, keyed the way a selection names it.

    GUT names a suite in its report by the file's path under ``res://`` (verified live against
    v9.7.1: ``<testsuite name="test/unit/test_x.gd">``), and appends the inner class for a suite
    written as one (``test_x.gd.TestThing``). A selection names the file, so an inner class's tests
    count toward its file rather than being a file of their own. Without that, the drop guard would
    expect far fewer tests than a selected run really produces, and read every one of them as a
    suite GUT had skipped.

    The cut is made at the **last** ``.gd.`` in the name, not the first ``.gd``. A directory whose
    own name holds those letters (``v1.gd_legacy/``) would otherwise end the path early, and every
    file under it would be filed as one that no selection ever names, which makes the drop guard
    quietly more forgiving there rather than louder. A name that is no path at all keeps itself.
    """
    per_file: dict[str, int] = {}
    for suite in suites:
        head, separator, _ = suite.name.rpartition(_INNER_CLASS_SEPARATOR)
        name = head + _GDSCRIPT_SUFFIX if separator else suite.name
        per_file[f"res://{name}"] = per_file.get(f"res://{name}", 0) + suite.tests
    return per_file


# The runners' defaults. DEFAULT_TIMEOUT is exposed so the CLI can present it (its --timeout
# default) from one source, without reading it off a class at parse time (which breaks when a test
# monkeypatches a runner). DEFAULT_REPORT_PATH / DEFAULT_GUT_REPORT_PATH are no longer surfaced
# anywhere outside this module: there is no CLI flag or .gdmutant.toml key for the report path any
# more, so each dataclass field default below is the sole place the report location is decided.
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
        pass. Abstract, with no permissive default (see the method): both adapters reject a
        zero-test report, and GUT additionally rejects a drop below its healthy baseline count (the
        shape only GUT produces — see the two class docstrings).
      * `_missing_report_error` — optionally, a better error for "this run wrote no report", when
        the framework names the cause in its own output (GdUnit4 does, for empty discovery).
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

    def command(  # pragma: no cover - overridden per adapter
        self, project_dir: str, *, markers: bool = False, files: Sequence[str] | None = None
    ) -> list[str]:
        """The ``godot --headless`` command that runs this framework's suite for `project_dir`.

        `markers` asks for the marker run's variant (`run_markers`). `files` restricts the run to
        those test files, in that order, instead of the whole configured test directory
        (`engine.runner.FileSelecting`); ``None`` runs the whole suite as it always did."""
        raise NotImplementedError

    def _result_from_report(  # pragma: no cover - overridden per adapter
        self,
        report_text: str,
        completed: subprocess.CompletedProcess[str],
        files: Sequence[str] | None = None,
    ) -> SuiteResult:
        """Parse this run's report into a `SuiteResult`, enforcing the adapter's **crash-safety**
        contract (`engine.runner.Runner`) as it does.

        `files` is the test files this run was restricted to, or ``None`` for the whole suite. A
        guard that compares against the whole suite's numbers has to be rescaled to them, or every
        selected run looks like a suite that failed to load (`engine.runner.FileSelecting`).

        **Abstract on purpose, with no parse-only default.** There used to be one, and GdUnit4
        inherited it — which is how the default runner ended up with no crash-safety enforcement of
        its own, resting entirely on a claim measured once. A permissive default is the wrong shape
        for this method: forgetting to override it produces no error, just a runner that quietly
        returns a pass for a run that never happened. Making it abstract turns that omission into a
        `NotImplementedError` the first time the adapter is used, so every new adapter has to state
        how *its* framework fails.
        """
        raise NotImplementedError

    def _missing_report_error(
        self, report: Path, completed: subprocess.CompletedProcess[str]
    ) -> RuntimeError:
        """The error `run` raises when this invocation wrote no report at all.

        Overridable so an adapter can name a cause it can actually *recognise* in the captured
        output. The base wording is deliberately hedged ("may have failed to run") because from the
        report's absence alone the cause is genuinely unknown: a Godot crash, a missing addon, or a
        mutant that broke loading all look identical here. `GdUnit4Runner` overrides it for the one
        case GdUnit4 announces by name.
        """
        detail = (completed.stderr or completed.stdout or "").strip()
        return RuntimeError(
            f"{self._framework} wrote no report at {report}. Godot may have failed to run"
            + (f":\n{detail[-1000:]}" if detail else "")
        )

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        """Run this framework's suite once against `project_dir` and return the parsed result.
        See `_execute`, which does the work."""
        return self._execute(project_dir, timeout, markers=False)

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        """One mutant against only `files` (`engine.runner.FileSelecting.run_selected`).

        The same path `run` takes, with the framework told which test files to run. Every guard
        `run` applies still applies: the report-freshness rule, the timeout, and the adapter's own
        crash-safety enforcement, which is handed the file list so a count it compares against the
        whole suite can be rescaled to this run's share of it.
        """
        return self._execute(project_dir, timeout, markers=False, files=files)

    def run_markers_files(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        """The marker run over exactly `files`, in that order
        (`engine.runner.FileSelecting.run_markers_files`). The reverse pass is what needs it."""
        return self._execute(project_dir, timeout, markers=True, files=files)

    def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        """The coverage marker run (`engine.runner.MarkerRunnable`, docs/decisions/0017): the whole
        suite once in the marked copy `project_dir`, with the framework told to keep going after a
        failure where it has such a switch, and any ``SCRIPT ERROR`` in the output reported in
        `SuiteResult.runtime_error`.

        That scan is the marker run's alone. Both frameworks already fail a test on a script error
        inside it, but not one outside every test (load time, between suites, after the summary),
        and such an error can cut short code that a test would otherwise have reached, which would
        make covered code look unreached. A mutant run does not need it: there, a script error
        outside a test cannot turn a kill into a survivor the way a missing marker hit can turn one
        into "no coverage"."""
        return self._execute(project_dir, timeout, markers=True)

    def _execute(
        self,
        project_dir: str,
        timeout: float | None,
        *,
        markers: bool,
        files: Sequence[str] | None = None,
    ) -> SuiteResult:
        """Run this framework's suite once against `project_dir` and return the parsed result.

        Deletes any stale report at `report_path` first and requires this run to write a fresh
        one, so a crash or hang can never be mistaken for a leftover pass. Raises
        `SourceOutsideProject` if `report_path` resolves outside `project_dir`, `SuiteTimeout` on a
        hang, and this adapter's `_missing_report_error` if no report appears at all.

        `files` restricts the run to those test files. There is deliberately one body for the whole
        suite and for a subset of it, rather than a second entry point beside it: a selected run is
        a second road to a mutant's verdict, and every guard here is one it must not miss.
        """
        # Checked before any work happens, including the import warm-up below: no reason to spend a
        # Godot boot on a run that's about to be refused anyway.
        project = Path(project_dir).resolve()
        report = (project / self.report_path).resolve()
        if not report.is_relative_to(project):
            # `Path.__truediv__` silently discards `project` if `report_path` is absolute, and
            # `../` walks upward the ordinary way. There is no CLI flag or config key left that can
            # set `report_path`, so this can only be reached by a direct, programmatic construction
            # of a runner — but every run below this point deletes whatever sits at `report` first
            # (the freshness guard, right below), so a report path that escapes the project is a
            # delete-anything primitive, not just a misconfiguration. Refuse instead of ever
            # resolving outside the project, as defense in depth. Reuses
            # SourceOutsideProject (loop.py's "path outside the project" case for a source file)
            # rather than a second, parallel exception for the same shape of problem.
            raise SourceOutsideProject(
                f"report path {self.report_path!r} resolves to {report}, outside the project "
                f"{project}. gdmutant deletes this file before every run to guarantee it reads "
                "this run's own result, never a stale one, so it refuses to point outside the "
                "project it was given."
            )
        # Warm Godot's import cache once (before the very first suite run) so the framework's
        # class_name types resolve on a cold checkout; a no-op if the engine already prepared — and
        # on every subsequent mutant.
        self.prepare(project_dir)
        budget = self.timeout if timeout is None else timeout
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
                self.command(project_dir, markers=markers, files=files),
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
            raise self._missing_report_error(report, completed)
        # Parse under the adapter's crash-safety contract — both adapters reject a zero-test report
        # rather than returning a pass; GUT additionally rejects a drop below its baseline count.
        result = self._result_from_report(report.read_text(encoding="utf-8"), completed, files)
        if not markers:
            return result
        # A newline between them, so a last stdout line with no newline of its own cannot run
        # into the first stderr line and hide where a SCRIPT ERROR starts.
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        return replace(result, runtime_error=script_error_excerpt(output))


@dataclass
class GdUnit4Runner(_GodotJUnitRunner):
    """Runs a project's GdUnit4 suite headlessly and parses the JUnit report.

    `test_path` is the GdUnit4 test directory (a ``res://`` path). `report_path` is where GdUnit4
    writes its JUnit XML, relative to the project dir. `godot` is the Godot executable.

    **Crash-safety (`engine.runner.Runner`) — and why it is shaped differently from GUT's.**
    GdUnit4 loads every suite in the scanned directory *up front*, during discovery, and a suite it
    cannot parse aborts the **whole run**: it prints "Script errors were detected during test
    discovery!", exits 105, and writes **no report**. It does not skip the broken suite and run the
    rest, which is exactly what GUT does. So the base's report-reappear guard is what catches a
    compile crash here, and no baseline-test-count-drop guard is needed: there is no
    healthy-suites-still-green report for a drop to be measured against.

    That is now **measured at n>1, not assumed**. ADR-0011 originally justified the guard with "a
    crash writes no report" observed against a single-suite corpus, which is the same n=1 evidence
    GUT also passed before its live probe proved it skips-and-continues. The corpus now carries a
    second, independent GdUnit4 suite (``corpus/test/test_independent.gd``) and
    ``tests/test_selftest_live.py`` runs the same probe GUT gets: healthy baseline, break
    ``turn_order.gd``, run again. Observed against GdUnit4 v6.1.3 + Godot 4.7 (2026-08-01): the run
    aborted at discovery with **no report**, and the healthy suite never ran.

    Two guards nonetheless, because "the version we measured" is not "the contract":
      * **``tests == 0`` (or an unparseable report) → error.** ``SuiteResult(0, 0, 0).failed`` is
        False, so a zero-test report reads as a clean pass and would mark the mutant SURVIVED. No
        GdUnit4 version we have run writes such a report — this exists so the *contract* does not
        rest on that, and it costs one comparison. A zero-test **report** is different from
        zero-test **discovery**: discovery writes nothing at all and is handled by
        `_missing_report_error` below, which is where a misconfigured ``--tests`` actually lands.
      * **No drop guard, deliberately.** GUT needs one because it skips-and-continues; GdUnit4 does
        not, on the evidence above, and inventing one would risk erroring on benign variance for a
        failure mode this framework does not have. The live probe is the trigger: if a future
        GdUnit4 starts skipping broken suites, the probe fails in gdmutant's own gate and *that* is
        when the guard gets widened — the same discipline `GutRunner`'s canary uses.
    """

    test_path: str = "res://test"
    report_path: str = DEFAULT_REPORT_PATH
    godot: str = "godot"
    timeout: float = DEFAULT_TIMEOUT
    _imported: bool = field(default=False, init=False, repr=False)
    _framework: ClassVar[str] = "GdUnit4"

    def command(
        self, project_dir: str, *, markers: bool = False, files: Sequence[str] | None = None
    ) -> list[str]:
        """The ``godot --headless`` command that runs the GdUnit4 suite for `project_dir`.

        `markers` adds ``-c`` (``--continue``) for the marker run. GdUnit4 stops at the first
        failing test by default, which would hide how much of the suite actually ran. A mutant run
        leaves it out: there, stopping at the first failure is the fast way to a kill.

        `files` replaces the single ``-a <test dir>`` with one ``-a`` per test file, in the order
        given, which is how GdUnit4 takes a chosen list (verified live against v6.1.3 + Godot 4.7:
        the suites run in exactly the order the flags appear, forwards and backwards).

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
        targets = [self.test_path] if files is None else list(files)
        return [
            self.godot,
            "--headless",
            "--path",
            str(Path(project_dir).resolve()),
            "-s",
            _GDUNIT_CMD_TOOL,
            *(flag for target in targets for flag in ("-a", target)),
            "-rc",
            "1",
            "--ignoreHeadlessMode",
            *(["-c"] if markers else []),
        ]

    def install_windows(self, project_dir: str, recorder_dir: str) -> None:
        """Write GdUnit4's file-window hook into the marked copy
        (`engine.runner.FileSelecting.install_windows`).

        GdUnit4 announces every suite it starts and finishes on one process-wide signal,
        ``GdUnitSignals.instance().gdunit_event``, which its own command-line runner listens to. So
        the hook is a small node that listens to the same signal and tells gdmutant's recorder when
        a window opens and closes. The recorder's writer autoload loads it by name, so nothing has
        to be passed on the command line.

        The alternative, injecting a ``before_test`` into every suite of the copy, would collide
        with any suite that already defines one and would have to be merged into user code. A
        signal that never fires is caught out loud by the window rules
        (`engine.coverage.window_problems`), not guessed at.
        """
        source = f"""extends Node
## gdmutant's GdUnit4 file-window hook, in gdmutant's throwaway marked copy of a project only.
## It tells `{MARKER_AUTOLOAD}` which test file is running, so each marker hit is credited to it.


func _ready() -> void:
\tGdUnitSignals.instance().gdunit_event.connect(_on_event)


func _on_event(event: GdUnitEvent) -> void:
\tif event.type() == GdUnitEvent.TESTSUITE_BEFORE:
\t\t{MARKER_AUTOLOAD}.begin_file(event.resource_path())
\telif event.type() == GdUnitEvent.TESTSUITE_AFTER:
\t\t{MARKER_AUTOLOAD}.end_file()
"""
        hook = Path(project_dir) / recorder_dir / WINDOW_HOOK_NAME
        hook.write_text(source, encoding="utf-8", newline="")

    def _result_from_report(
        self,
        report_text: str,
        completed: subprocess.CompletedProcess[str],
        files: Sequence[str] | None = None,
    ) -> SuiteResult:
        """Crash-safety (see the class docstring): a report GdUnit4 *did* write, but describing zero
        tests, is an error rather than a pass.

        ``SuiteResult(0, 0, 0).failed`` is False, so returning it would mark the mutant SURVIVED off
        a run in which nothing executed. No GdUnit4 version measured here writes that report — it
        writes none at all when discovery comes up empty (`_missing_report_error`) and none at all
        when a suite fails to parse. The guard exists so the crash-safety contract holds by
        construction rather than by that observation, which is the assumption that made the GUT
        false-survivor possible in the first place.
        """
        try:
            result = parse_junit_xml(report_text)
        except ValueError:
            result = None  # a report with no <testsuite> at all — a run that described nothing
        if result is None or result.tests == 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            # Name what actually ran. Under selection that is a handful of chosen files, not the
            # test directory, and sending a reader to look at the wrong scope is how a real fault
            # gets chased in the wrong place.
            scope = self.test_path if files is None else f"the {len(files)} selected test files"
            raise RuntimeError(
                f"GdUnit4 wrote a report describing 0 tests under {scope}, so nothing "
                "actually ran. That cannot be a passing suite, and treating it as one would report "
                "this mutant as SURVIVED off a run that never happened"
                + (f":\n{detail[-1000:]}" if detail else "")
            )
        return result

    def _missing_report_error(
        self, report: Path, completed: subprocess.CompletedProcess[str]
    ) -> RuntimeError:
        """The base's no-report error, except when GdUnit4 said *why* — then say that instead.

        GdUnit4 exits **0** and writes no report when discovery finds no suites under ``-a``,
        printing `_GDUNIT_NO_TESTS_MARKER`. The base's wording ("Godot may have failed to run")
        sends that user to debug a crash that is not happening and never names the flag that fixes
        it — the same wrong-diagnosis problem `GutRunner` solves for its own zero-test baseline.
        Here the framework announces the cause, so this reads it rather than guessing from run
        order.

        Deliberately does **not** contain "wrote no report": `cli._gdunit4_addon_hint` keys on that
        phrase to offer "install the GdUnit4 addon", which would be wrong advice here — printing
        this marker at all proves the addon loaded.
        """
        output = (completed.stdout or "") + (completed.stderr or "")
        if _GDUNIT_NO_TESTS_MARKER not in output:
            return super()._missing_report_error(report, completed)
        return RuntimeError(
            f"GdUnit4 discovered no test suites under {self.test_path}, so it ran nothing and "
            "produced no report. This is test discovery, not a crash: GdUnit4 said so "
            f"itself ({_GDUNIT_NO_TESTS_MARKER!r}). Point gdmutant at the directory holding your "
            "suites with --tests res://<your test dir>, and check they are GdUnit4 suites "
            "(extending GdUnitTestSuite)"
        )


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
    #: Each test file's own test count, from the same healthy baseline run that fixed
    #: `_baseline_tests`, keyed by the ``res://`` path a selection names it by. It is what the drop
    #: guard rescales to under selection: a run of three files out of thirty is expected to be
    #: exactly those three files' tests, not the whole suite's. Written once, on the baseline run,
    #: and only read afterwards, so the shared instance stays safe across ``--jobs`` workers.
    _file_tests: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    #: Where the marker run's file-window hook was installed, as a ``res://`` path, or ``None``
    #: when none was (``--coverage-analysis off`` or ``all``). Set by `install_windows` before any
    #: pass runs, and read only by the marker passes, which are serial.
    _window_hook: str | None = field(default=None, init=False, repr=False)
    _framework: ClassVar[str] = "GUT"

    def command(
        self, project_dir: str, *, markers: bool = False, files: Sequence[str] | None = None
    ) -> list[str]:
        """The ``godot --headless`` command that runs the GUT suite for `project_dir`.

        `markers` adds the file-window hook as GUT's pre-run script, when one was installed. GUT
        already runs every test after a failure, so the marker run needs no "keep going" switch.

        `files` replaces ``-gdir`` with one ``-gtest=`` per test file, in the order given, which is
        how GUT takes a chosen list (verified live against v9.7.1 + Godot 4.7: the scripts run in
        exactly the order the flags appear, forwards and backwards).

        GUT's command-line flags are ``=``-joined (``-gdir=…``), not space-separated. ``-gexit``
        makes GUT quit when the run finishes (headless CI mode); ``-gjunit_xml_file`` writes the
        JUnit report the engine reads. The report is overwritten in place each run, so — unlike
        GdUnit4 — there is no report-history flag to force.
        """
        # Resolve --path to an absolute path for the same reason as GdUnit4 (run() sets
        # cwd=project_dir; a relative --path would be applied twice).
        targets = (
            [f"-gdir={self.test_dir}"] if files is None else [f"-gtest={file}" for file in files]
        )
        hook = [f"-gpre_run_script={self._window_hook}"] if markers and self._window_hook else []
        return [
            self.godot,
            "--headless",
            "--path",
            str(Path(project_dir).resolve()),
            "-s",
            _GUT_CMD_TOOL,
            *targets,
            f"-gjunit_xml_file=res://{self.report_path}",
            "-gexit",
            *hook,
        ]

    def install_windows(self, project_dir: str, recorder_dir: str) -> None:
        """Write GUT's file-window hook into the marked copy
        (`engine.runner.FileSelecting.install_windows`).

        GUT's own ``gut`` object emits ``start_script`` and ``end_script`` around every test file,
        and GUT hands a pre-run hook script that object, so the hook is a `GutHookScript` that
        connects the two signals to gdmutant's recorder. It is named on the command line with
        ``-gpre_run_script``, which is also why it is not the file the recorder's writer autoload
        loads by name: a `GutHookScript` is not a node, and only GUT knows how to run one.

        The alternative, injecting a ``before_each`` into every suite of the copy, would collide
        with any suite that already defines one. A hook that never fires is caught out loud by the
        window rules (`engine.coverage.window_problems`), not guessed at.
        """
        source = f"""extends GutHookScript
## gdmutant's GUT file-window hook, in gdmutant's throwaway marked copy of a project only.
## It tells `{MARKER_AUTOLOAD}` which test file is running, so each marker hit is credited to it.


func run() -> void:
\tgut.start_script.connect(_on_start_script)
\tgut.end_script.connect(_on_end_script)


func _on_start_script(script_obj) -> void:
\t## `path`, not `get_full_name()`: the latter appends the inner class, and GUT runs every inner
\t## class of a test script as a suite of its own. The file is what gdmutant hands back to it.
\t{MARKER_AUTOLOAD}.begin_file(str(script_obj.path))


func _on_end_script() -> void:
\t{MARKER_AUTOLOAD}.end_file()
"""
        hook = Path(project_dir) / recorder_dir / _GUT_WINDOW_HOOK_NAME
        hook.write_text(source, encoding="utf-8", newline="")
        self._window_hook = f"res://{Path(recorder_dir).as_posix()}/{_GUT_WINDOW_HOOK_NAME}"

    def _expected_tests(self, files: Sequence[str] | None) -> int | None:
        """How many tests this run should collect: the whole baseline, or the selected files' share.

        ``None`` until a baseline has run. A selected file the baseline never reported is left out
        of the sum, which lowers the floor rather than raising it: an unknown file can only make
        the guard more forgiving, never turn a healthy run into an error.
        """
        if self._baseline_tests is None:
            return None
        if files is None:
            return self._baseline_tests
        return sum(self._file_tests.get(file, 0) for file in files)

    def _result_from_report(
        self,
        report_text: str,
        completed: subprocess.CompletedProcess[str],
        files: Sequence[str] | None = None,
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
        baseline = self._expected_tests(files)
        is_baseline = baseline is None
        if baseline is None:
            # First run = the engine's healthy baseline (run serially before any --jobs fan-out): it
            # fixes the expected count, whole and per file. Later runs only read it, so the shared
            # instance is safe. A selected run is never the first: selection needs a marker run,
            # which needs a baseline.
            self._baseline_tests = tests
            self._file_tests = _tests_per_file(result.suites if result else ())
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
                    "this is test discovery, not a broken suite: no mutant existed yet. GUT's "
                    "-gdir does not search subdirectories, and GUT's own layout puts suites in "
                    "test/unit/: point gdmutant at them with --tests res://test/unit (or wherever "
                    "yours live). One directory only. For a tree of suites, run GUT yourself with "
                    "-ginclude_subdirs via --runner command"
                )
            else:
                # Under selection the floor is the selected files' own tests, not the whole
                # suite's, so say which of the two this run was measured against.
                scope = (
                    "the baseline" if files is None else f"the {len(files)} test files it was given"
                )
                reason = (
                    "GUT ran 0 tests"
                    if tests == 0
                    else f"GUT ran {tests} tests, fewer than the {baseline} {scope} expects"
                )
                message = (
                    f"{reason}: a test suite failed to compile/load and GUT skipped it (it runs "
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
            "warning: test collection was non-deterministic. A run collected more tests than the "
            "healthy baseline, which a legitimate mutant cannot cause (a mutation cannot add test "
            "files). The crash-safety guard's protection against a silently-masked skipped test "
            "suite is degraded in this environment; investigate flaky suite loading. This is the "
            "trigger to build per-suite baseline tracking. The mutation score and exit code are "
            "unchanged."
        )
