"""Tests for the GUT runner (subprocess mocked — no real Godot).

GUT is a peer JUnit adapter to GdUnit4 over the shared `_GodotJUnitRunner` contract (ADR-0011), so
these mirror `test_gdunit_runner.py` — same behaviour, GUT's own flags — plus the one GUT-specific
hardening: the ``tests == 0 → ERROR`` crash-safety guard (a false-survivor regression test)."""

import subprocess
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GutRunner
from gdmutant.engine.runner import Runner, RunWarning, SuiteTimeout


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "reports" / "gut_results.xml"
    path.parent.mkdir(parents=True)
    return path


def test_command_construction(tmp_path: Path) -> None:
    # command() resolves --path deliberately (see runner.py), so a hardcoded POSIX literal cannot
    # survive it on Windows. tmp_path is already absolute and platform-native, so the flag list and
    # its order stay pinned exactly while the path itself stops asserting which OS the suite runs.
    proj = tmp_path / "proj"
    proj.mkdir()
    cmd = GutRunner(test_dir="res://gut_test", godot="godot4").command(str(proj))
    assert cmd == [
        "godot4",
        "--headless",
        "--path",
        str(proj),
        "-s",
        "res://addons/gut/gut_cmdln.gd",
        "-gdir=res://gut_test",  # GUT's flags are =-joined, not space-separated
        "-gjunit_xml_file=res://reports/gut_results.xml",  # the JUnit report GUT writes
        "-gexit",  # quit when the run finishes (headless CI mode)
    ]


def test_command_resolves_a_relative_project_dir_to_absolute() -> None:
    # run() sets cwd=project_dir, so a relative --path would be applied twice (Godot would look for
    # 'corpus/corpus' and abort). The --path arg must be absolute so it is cwd-independent.
    cmd = GutRunner().command("corpus")
    path_arg = cmd[cmd.index("--path") + 1]
    assert Path(path_arg).is_absolute()
    assert Path(path_arg) == Path("corpus").resolve()


def test_run_parses_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report(tmp_path)
    # GUT wraps its suites in <testsuites>; the parser sums the child <testsuite> elements.
    xml = '<testsuites><testsuite tests="4" failures="1" errors="0"/></testsuites>'
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: report.write_text(xml))

    result = GutRunner().run(str(tmp_path))

    assert (result.tests, result.failures) == (4, 1)
    assert result.failed is True


def test_run_invokes_subprocess_with_the_constructed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Assert run() actually USES the constructed command, runs in the project dir, honours the
    # timeout, and passes check=False (GUT exits non-zero on failures — the report decides).
    report = _report(tmp_path)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        report.write_text('<testsuites><testsuite tests="1" failures="0"/></testsuites>')

    monkeypatch.setattr(runner_mod.subprocess, "run", capture)
    runner = GutRunner(test_dir="res://t", godot="godot4", timeout=42.0)
    runner.run(str(tmp_path))

    # run() also issues a one-time --import warm-up; assert against the *suite* call specifically.
    suite_calls = [(a, k) for (a, k) in calls if "--import" not in a[0]]
    (args, kwargs) = suite_calls[0]
    assert args[0] == runner.command(str(tmp_path))
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 42.0
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_run_reflects_latest_report_on_repeated_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine calls run() many times against the same project; each run must reflect its OWN
    # result — never a stale earlier one.
    report = _report(tmp_path)
    outcomes = iter(
        [
            '<testsuites><testsuite tests="3" failures="0"/></testsuites>',  # baseline: passes
            '<testsuites><testsuite tests="3" failures="1"/></testsuites>',  # mutant: fails
        ]
    )

    def fake_run(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        if "--import" in command:
            return
        report.write_text(next(outcomes), encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    assert runner.run(str(tmp_path)).failed is False
    assert runner.run(str(tmp_path)).failed is True


def test_run_does_not_return_a_stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A previous mutant's (passing) report is on disk, and THIS invocation writes nothing (a Godot
    # crash / addon-load failure). It must raise, not silently return the stale passing verdict.
    report = _report(tmp_path)
    report.write_text('<testsuites><testsuite tests="9" failures="0"/></testsuites>', "utf-8")
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "")
    )

    with pytest.raises(RuntimeError, match="no report"):
        GutRunner().run(str(tmp_path))


def test_run_raises_when_no_report_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "")
    )
    with pytest.raises(RuntimeError, match="GUT wrote no report"):
        GutRunner().run(str(tmp_path))


def test_run_surfaces_godot_output_when_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "SCRIPT ERROR: boom"),
    )
    with pytest.raises(RuntimeError, match="SCRIPT ERROR: boom"):
        GutRunner().run(str(tmp_path))


def test_run_treats_zero_tests_as_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # THE GUT-specific crash-safety guard (a false-survivor regression test). On a test-file compile
    # crash GUT writes an EMPTY <testsuites tests="0"/> and exits 0. Parsed naively that is a clean
    # zero-test pass -> the responsible mutant would be marked SURVIVED (a wrong survivor — the
    # worst failure). The adapter must instead surface tests == 0 as an execution ERROR.
    report = _report(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text('<testsuites tests="0"></testsuites>', encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="no tests"):
        GutRunner().run(str(tmp_path))


def test_zero_tests_error_for_a_parseable_zero_test_suite_surfaces_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The OTHER empty-report shape: a child <testsuite tests="0"> that DOES parse (tests == 0), so
    # this exercises the non-ValueError branch of the guard — and the error tails Godot's captured
    # output so a compile crash is diagnosable.
    report = _report(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text('<testsuites><testsuite tests="0"/></testsuites>', encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "SCRIPT ERROR: bad script")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    # The detail is appended after a newline, so match across it ([\s\S], not . which stops at \n).
    with pytest.raises(RuntimeError, match=r"no tests[\s\S]*SCRIPT ERROR: bad script"):
        GutRunner().run(str(tmp_path))


def test_zero_tests_on_the_baseline_run_reports_discovery_not_a_compile_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Confirmed live (GUT v9.7.1 on Godot 4.7): --tests defaults to res://test, GUT's own layout
    # puts suites in test/unit/, and -gdir does NOT recurse — so a stock GUT project collects zero
    # tests on the BASELINE run, before any mutant exists. Nothing is broken, so the compile/load
    # message is wrong there: it sends the user to debug a crash that isn't happening. The baseline
    # case must name the cause (discovery) and the flag that fixes it.
    report = _report(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text('<testsuites><testsuite tests="0"/></testsuites>', encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        GutRunner().run(str(tmp_path))
    message = str(excinfo.value)
    assert "res://test/unit" in message  # the flag value that fixes the stock layout
    assert "-gdir does not search subdirectories" in message  # why the default came up empty
    assert "res://test" in message  # the directory it actually searched
    assert "compile" not in message  # NOT the mid-run diagnosis — nothing failed to load


def test_zero_tests_after_a_healthy_baseline_keeps_the_compile_crash_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mid-run drop-to-zero: the baseline collected tests, so discovery demonstrably works and a
    # later empty report IS the compile/load skip. That message must survive the baseline-case
    # rewording above, or the false-survivor guard loses its diagnosis.
    report = _report(tmp_path)
    counts = iter(["4", "0"])

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text(
                f'<testsuites><testsuite tests="{next(counts)}" failures="0"/></testsuites>',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    assert runner.run(str(tmp_path)).passed  # baseline: 4 tests — discovery works
    with pytest.raises(RuntimeError, match="GUT ran 0 tests: a test suite failed to compile/load"):
        runner.run(str(tmp_path))


def test_run_raises_suite_timeout_on_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A GUT run that outruns its budget surfaces as SuiteTimeout (a detection), not a leaked
    # subprocess.TimeoutExpired or a no-report RuntimeError.
    def boom(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="godot", timeout=1.0)

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    with pytest.raises(SuiteTimeout):
        GutRunner(timeout=1.0).run(str(tmp_path))


def test_run_warms_the_import_cache_once_before_the_first_suite_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a cold checkout GUT's class_names don't resolve until Godot's --import scan writes the
    # global class cache ("Some GUT class_names have not been imported"), so the runner must warm it
    # — once — before the first suite run. Assert the --import fires first, exactly once.
    report = _report(tmp_path)
    calls: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        calls.append(command)
        if "--import" not in command:
            report.write_text('<testsuites><testsuite tests="1" failures="0"/></testsuites>')

    monkeypatch.setattr(runner_mod.subprocess, "run", capture)
    runner = GutRunner(godot="godot4")
    runner.run(str(tmp_path))
    runner.run(str(tmp_path))  # second mutant: must NOT re-import

    import_calls = [c for c in calls if "--import" in c]
    assert len(import_calls) == 1, f"expected exactly one --import warm-up, got {len(import_calls)}"
    assert calls[0] == ["godot4", "--headless", "--path", str(tmp_path.resolve()), "--import"]
    assert "--import" not in calls[1]  # the suite run follows the warm-up


def test_run_errors_when_test_count_drops_below_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE widening the live probe proved necessary: real GUT SKIPS a suite whose source-under-test
    # won't compile and runs the rest green (tests>0, failures=0) — a false survivor the tests==0
    # guard misses. The first (baseline) run fixes the expected count; a later run with FEWER tests
    # is that skipped suite and must ERROR, never pass. Here: baseline 5 tests, then a run with 2.
    report = _report(tmp_path)
    counts = iter(["5", "2"])

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text(
                f'<testsuites><testsuite tests="{next(counts)}" failures="0"/></testsuites>',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    assert runner.run(str(tmp_path)).passed  # baseline: 5 tests, all suites loaded
    with pytest.raises(RuntimeError, match="fewer than the baseline"):
        runner.run(str(tmp_path))  # 2 tests -> a suite was skipped -> error, not a false survivor


def test_run_does_not_error_when_test_count_holds_at_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A healthy mutant keeps every suite loadable, so the count holds at the baseline — that must
    # NOT trip the drop guard (only a real drop does). Baseline 5 pass, then 5 with a failure ->
    # killed, not error. Guards against the drop check misfiring on the common (equal-count) case.
    report = _report(tmp_path)
    reports = iter(
        [
            '<testsuites><testsuite tests="5" failures="0"/></testsuites>',  # baseline: healthy
            '<testsuites><testsuite tests="5" failures="1"/></testsuites>',  # mutant: killed
        ]
    )

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text(next(reports), encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    assert runner.run(str(tmp_path)).passed
    assert runner.run(str(tmp_path)).failed  # 5 tests, 1 failure -> killed (no false drop-error)


def test_run_creates_the_report_directory_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GUT does not create the -gjunit_xml_file parent dir; on a fresh project (no reports/ dir) it
    # runs green but fails to export, writing no report -> "wrote no report" on every run. So run()
    # must create the parent dir first. Here the reports/ dir does NOT pre-exist; the mock writes
    # the report (which only succeeds if run() already made the dir), proving the mkdir happened.
    report = tmp_path / "reports" / "gut_results.xml"
    assert not report.parent.exists()  # precondition: the dir is missing, as on a fresh checkout

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text('<testsuites><testsuite tests="2" failures="0"/></testsuites>')
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    result = GutRunner().run(str(tmp_path))
    assert report.parent.is_dir()  # run() created it before invoking GUT
    assert (result.tests, result.failed) == (2, False)


def test_run_refuses_a_report_path_that_escapes_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Proof of the fix, not just its shape: a decoy file OUTSIDE the project survives run() instead
    # of being deleted. Before the containment check, `report.unlink(missing_ok=True)` ran on
    # whatever `Path(project_dir) / report_path` resolved to, with nothing stopping report_path
    # from walking out via `../..` (or, worse, an absolute path — pathlib silently discards the
    # left side of `/` when the right side is absolute, so an absolute report_path replaced
    # project_dir entirely). A `.gdmutant.toml` in a cloned project used to be able to set
    # report-path with no --trust-config needed, making this a
    # delete-anything-on-the-victim's-machine primitive on a plain `gdmutant run` with no flags at
    # all — that config key is gone now, but the containment check stays as defense in depth even
    # though nothing public can set report_path any more.
    project = tmp_path / "project"
    project.mkdir()
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("precious", encoding="utf-8")

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must never run once the path check refuses")

    monkeypatch.setattr(runner_mod.subprocess, "run", fail_if_called)
    runner = GutRunner(report_path="../decoy.txt")
    with pytest.raises(runner_mod.SourceOutsideProject, match="outside the project"):
        runner.run(str(project))
    assert decoy.read_text(encoding="utf-8") == "precious"  # never touched


def test_run_refuses_an_absolute_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other escape route: pathlib's `/` discards the left operand entirely when the right side
    # is absolute, so `Path(project_dir) / "/etc/passwd"`-shaped input bypassed containment even
    # though it contains no `..`.
    project = tmp_path / "project"
    project.mkdir()
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("precious", encoding="utf-8")

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("subprocess must never run once the path check refuses")

    monkeypatch.setattr(runner_mod.subprocess, "run", fail_if_called)
    runner = GutRunner(report_path=str(decoy))
    with pytest.raises(runner_mod.SourceOutsideProject, match="outside the project"):
        runner.run(str(project))
    assert decoy.read_text(encoding="utf-8") == "precious"


def test_run_warns_but_does_not_error_when_test_count_rises_above_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE non-determinism canary — symmetric to the drop guard above. A legitimate mutant can never
    # raise the collected test count ABOVE the healthy baseline (a mutation cannot add test files),
    # so a later run reporting MORE tests deterministically proves the baseline undercounted: suite
    # collection is non-deterministic. That must surface as a run-level WARNING (run_warning), NOT a
    # per-mutant error — flipping the mutant to error would false-error on benign flakiness. Here:
    # baseline 3 tests, then a run of 5. The mutant must still be a PASS, and the canary must fire.
    report = _report(tmp_path)
    counts = iter(["3", "5"])

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text(
                f'<testsuites><testsuite tests="{next(counts)}" failures="0"/></testsuites>',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    assert runner.run(str(tmp_path)).passed  # baseline: 3 tests
    assert runner.run(str(tmp_path)).passed  # 5 tests -> a PASS, never an error (the key assertion)
    warning = runner.run_warning()
    assert warning is not None  # the canary fired
    assert "non-deterministic" in warning
    assert "per-suite baseline tracking" in warning  # names the deferred widening it triggers


def test_run_warning_is_silent_when_test_count_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stable suite never trips the canary: run_warning stays None across equal-count runs, so it
    # cannot cry wolf on a healthy environment (guards the > baseline branch from misfiring).
    report = _report(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text('<testsuites><testsuite tests="4" failures="0"/></testsuites>')
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GutRunner()
    runner.run(str(tmp_path))  # baseline: 4
    runner.run(str(tmp_path))  # 4 again -> no rise, no canary
    assert runner.run_warning() is None


def test_gut_runner_satisfies_the_protocol() -> None:
    assert isinstance(GutRunner(), Runner)


def test_gut_runner_satisfies_the_run_warning_protocol() -> None:
    # GUT surfaces the non-determinism canary via the optional RunWarning contract, so the CLI can
    # emit it generically (isinstance) without naming the framework.
    assert isinstance(GutRunner(), RunWarning)
