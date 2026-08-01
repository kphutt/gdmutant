"""Tests for the GdUnit4 runner (subprocess mocked — no real Godot)."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.runner import Runner, SuiteTimeout, with_filename


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "reports" / "report_1" / "results.xml"
    path.parent.mkdir(parents=True)
    return path


def _writes(report: Path, xml: str, stderr: str = "") -> Callable[..., subprocess.CompletedProcess]:
    """A fake `subprocess.run` that writes `xml` as this invocation's report (the `--import` warm-up
    writes nothing, as the real one doesn't) and returns a real `CompletedProcess` — the runner
    reads its captured output when a guard fires, so a stand-in that returns anything else hides
    the very branch these tests exercise."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        command = args[0]
        assert isinstance(command, list)
        if "--import" not in command:
            report.write_text(xml, encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", stderr)

    return fake_run


def test_command_construction(tmp_path: Path) -> None:
    # command() resolves --path deliberately (see runner.py), so a hardcoded POSIX literal cannot
    # survive it on Windows: "/proj" resolves to "C:\proj" there. tmp_path is already absolute and
    # platform-native, so the flag list and its order stay pinned exactly while the path itself
    # stops being an assertion about which OS the suite happens to run on.
    proj = tmp_path / "proj"
    proj.mkdir()
    cmd = GdUnit4Runner(test_path="res://test", godot="godot4").command(str(proj))
    assert cmd == [
        "godot4",
        "--headless",
        "--path",
        str(proj),
        "-s",
        "res://addons/gdUnit4/bin/GdUnitCmdTool.gd",
        "-a",
        "res://test",
        "-rc",
        "1",  # report-count=1: overwrite one report dir (don't increment) — see runner.py
        "--ignoreHeadlessMode",  # modern GdUnit4 refuses --headless without it (verified live)
    ]


def test_command_resolves_a_relative_project_dir_to_absolute() -> None:
    # run() sets cwd=project_dir, so a relative --path would be applied twice (Godot would look for
    # 'corpus/corpus' and abort). The --path arg must be absolute so it is cwd-independent.
    cmd = GdUnit4Runner().command("corpus")
    path_arg = cmd[cmd.index("--path") + 1]
    assert Path(path_arg).is_absolute()
    assert Path(path_arg) == Path("corpus").resolve()


def test_run_parses_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report(tmp_path)
    xml = '<testsuite tests="4" failures="1" errors="0"/>'
    # The mock stands in for Godot writing the report this invocation.
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: report.write_text(xml))

    result = GdUnit4Runner().run(str(tmp_path))

    assert (result.tests, result.failures) == (4, 1)
    assert result.failed is True


def test_run_invokes_subprocess_with_the_constructed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The subprocess seam is the tool's only bridge to real Godot. Assert run() actually USES the
    # constructed command, runs in the project dir, honors the timeout, and passes check=False
    # (GdUnit4 exits non-zero on test failures — the report decides, not the exit code). Without
    # this, a run() that shelled out to the wrong command would pass every other test.
    report = _report(tmp_path)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        report.write_text('<testsuite tests="1" failures="0"/>', encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", capture)
    runner = GdUnit4Runner(test_path="res://t", godot="godot4", timeout=42.0)
    runner.run(str(tmp_path))

    # run() also issues a one-time --import warm-up; assert against the *suite* call specifically.
    suite_calls = [(a, k) for (a, k) in calls if "--import" not in a[0]]
    (args, kwargs) = suite_calls[0]
    assert args[0] == runner.command(str(tmp_path))
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 42.0
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True  # per-mutant chatter is captured, not inherited
    assert kwargs["text"] is True


def test_run_reflects_latest_report_on_repeated_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine calls run() many times against the same project; each run must reflect its OWN
    # result — never a stale earlier one.
    report = _report(tmp_path)
    outcomes = iter(
        [
            '<testsuite tests="3" failures="0" errors="0"/>',  # baseline: passes
            '<testsuite tests="3" failures="1" errors="0"/>',  # mutant: fails
        ]
    )

    def fake_run(*args: object, **kwargs: object) -> None:
        # The one-time --import warm-up writes no report; only suite runs advance the outcomes.
        command = args[0]
        assert isinstance(command, list)
        if "--import" in command:
            return
        report.write_text(next(outcomes), encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = GdUnit4Runner()
    assert runner.run(str(tmp_path)).failed is False
    assert runner.run(str(tmp_path)).failed is True


def test_run_does_not_return_a_stale_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A previous mutant's (passing) report is on disk, and THIS invocation writes nothing (a Godot
    # crash / addon-load failure). It must raise, not silently return the stale passing verdict —
    # the tool's worst failure mode (NF-5).
    report = _report(tmp_path)
    report.write_text('<testsuite tests="9" failures="0" errors="0"/>', encoding="utf-8")
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "")
    )

    with pytest.raises(RuntimeError, match="no report"):
        GdUnit4Runner().run(str(tmp_path))


def test_run_raises_when_no_report_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "")
    )
    with pytest.raises(RuntimeError, match="no report"):
        GdUnit4Runner().run(str(tmp_path))


def test_run_surfaces_godot_output_when_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When Godot writes no report, its captured stderr is included in the error so the failure can
    # be diagnosed instead of vanishing. Pinned as `endswith(":\n" + output)` rather than a bare
    # substring match, so the join itself is tested: a substring match still passes when the
    # separator is mangled and the output is buried mid-sentence.
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "SCRIPT ERROR: boom"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner().run(str(tmp_path))
    assert str(excinfo.value).endswith(":\nSCRIPT ERROR: boom"), excinfo.value


def test_the_no_report_error_ends_cleanly_when_godot_captured_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both of this runner's error paths append ":\n<tail of Godot's output>" only when there IS
    # output. With none, the message has to end at its own last word. This is what
    # `test_loop.py::test_baseline_failure_raises` pins for the engine's baseline message, and the
    # runner had no equivalent: a mutant that turned the empty-output fallback into junk appended
    # ":\njunk" with the whole suite still green (found by the line-scoped mutation run for this
    # change).
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "")
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner().run(str(tmp_path))
    assert str(excinfo.value).endswith("Godot may have failed to run"), excinfo.value


def test_the_zero_test_report_error_ends_cleanly_when_godot_captured_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The peer of the test above, for the other error path. Same empty-output branch, same
    # requirement: no dangling ":" and no junk after the last word.
    report = _report(tmp_path)
    monkeypatch.setattr(
        runner_mod.subprocess, "run", _writes(report, '<testsuite tests="0" failures="0"/>')
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner().run(str(tmp_path))
    assert str(excinfo.value).endswith("off a run that never happened"), excinfo.value


def test_run_treats_a_zero_test_report_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SuiteResult(0, 0, 0).failed is False, so a zero-test report would read as a clean pass and
    # mark the mutant SURVIVED off a run in which nothing executed — gdmutant's worst failure mode.
    # No measured GdUnit4 writes this report (it writes none at all), which is exactly why the
    # contract must not depend on that observation: GUT was assumed safe on the same evidence.
    report = _report(tmp_path)
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        _writes(report, '<testsuite tests="0" failures="0" errors="0"/>'),
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner(test_path="res://mysuites").run(str(tmp_path))
    message = str(excinfo.value)
    assert "0 tests" in message, message
    # Names the directory it actually scanned, the way the discovery error below does. Without this
    # the message could stop identifying which path came back empty and no test would notice.
    assert "res://mysuites" in message, message


def test_run_treats_a_report_with_no_testsuite_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other zero shape: XML that parses but describes no suite at all. parse_junit_xml raises
    # ValueError on it, which must become the same crash-safety error, never a leaked ValueError.
    report = _report(tmp_path)
    monkeypatch.setattr(runner_mod.subprocess, "run", _writes(report, '<testsuites tests="0"/>'))
    with pytest.raises(RuntimeError, match="0 tests"):
        GdUnit4Runner().run(str(tmp_path))


def test_zero_test_report_error_surfaces_godot_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The captured output is what makes the error diagnosable rather than a bare assertion. Same
    # `endswith(":\n" + output)` shape as the no-report peer above, for the same reason.
    report = _report(tmp_path)
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        _writes(report, '<testsuite tests="0" failures="0"/>', stderr="SCRIPT ERROR: boom"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner().run(str(tmp_path))
    assert str(excinfo.value).endswith(":\nSCRIPT ERROR: boom"), excinfo.value


def test_empty_discovery_is_reported_as_discovery_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GdUnit4 exits 0 and writes NO report when discovery finds no suites under -a, printing "No
    # test cases found". The generic "Godot may have failed to run" would send the user to debug a
    # crash that is not happening and never name the flag that fixes it.
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "No test cases found, abort test run!", ""
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        GdUnit4Runner(test_path="res://wrong").run(str(tmp_path))
    message = str(excinfo.value)
    assert "discovered no test suites" in message
    assert "res://wrong" in message  # names the path it actually scanned
    assert "--tests" in message  # names the flag that fixes it
    # Quotes GdUnit4's own words. The whole claim "this is discovery, not a crash" rests on the
    # framework having said so, so the message has to show the evidence rather than assert it.
    assert runner_mod._GDUNIT_NO_TESTS_MARKER in message
    # Must NOT match cli._gdunit4_addon_hint's trigger: printing this marker proves the addon
    # loaded, so "install the GdUnit4 addon" would be actively wrong advice.
    assert "wrote no report" not in message


def test_empty_discovery_is_recognised_on_stderr_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The marker is looked for in stdout AND stderr, because which stream Godot lands it on is not
    # gdmutant's to decide: it moves with the build, the platform, and any console redirection.
    # v6.1.3 put it on stdout, so only that half was pinned, and the stderr half of the
    # concatenation could be deleted with the whole suite still green — caught by the line-scoped
    # mutation run for this change, which is exactly the "one path enforces less" shape.
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "", "No test cases found, abort test run!"
        ),
    )
    with pytest.raises(RuntimeError, match="discovered no test suites"):
        GdUnit4Runner(test_path="res://wrong").run(str(tmp_path))


def test_a_no_report_run_without_the_discovery_marker_keeps_the_generic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The discovery hint is a special case, not a replacement: a genuine crash (no marker) must
    # still raise the hedged "wrote no report" error, which is what cli._gdunit4_addon_hint keys on
    # to offer the missing-addon advice.
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "SCRIPT ERROR: crashed"),
    )
    with pytest.raises(RuntimeError, match="wrote no report"):
        GdUnit4Runner().run(str(tmp_path))


def test_run_raises_suite_timeout_on_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A GdUnit4 run that outruns its budget surfaces as SuiteTimeout (a detection), not a leaked
    # subprocess.TimeoutExpired or a no-report RuntimeError.
    def boom(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="godot", timeout=1.0)

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    with pytest.raises(SuiteTimeout):
        GdUnit4Runner(timeout=1.0).run(str(tmp_path))


def test_run_warms_the_import_cache_once_before_the_first_suite_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a cold checkout GdUnit4's class_name types don't resolve until Godot's --import scan writes
    # the global class cache, so the runner must warm it — once — before the very first suite run,
    # or the baseline fails to even load the tool. Assert the --import fires first, and
    # exactly once across repeated runs (the cache persists across mutants).
    report = _report(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def capture(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        calls.append((command, kwargs))
        if "--import" not in command:
            report.write_text('<testsuite tests="1" failures="0"/>', encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", capture)
    runner = GdUnit4Runner(godot="godot4")
    runner.run(str(tmp_path))
    runner.run(str(tmp_path))  # second mutant: must NOT re-import

    commands = [c for (c, _) in calls]
    import_calls = [(c, k) for (c, k) in calls if "--import" in c]
    assert len(import_calls) == 1, f"expected exactly one --import warm-up, got {len(import_calls)}"
    # It runs before the first suite command, and targets the resolved project path.
    assert commands[0] == ["godot4", "--headless", "--path", str(tmp_path.resolve()), "--import"]
    assert "--import" not in commands[1]  # the suite run follows the warm-up
    # The warm-up must not check the exit code (--import routinely exits non-zero on benign import
    # chatter, and only TimeoutExpired is suppressed — check=True would raise and abort the run),
    # and must capture output so that chatter stays off the console.
    (_, import_kwargs) = import_calls[0]
    assert import_kwargs["check"] is False
    assert import_kwargs["capture_output"] is True
    assert import_kwargs["text"] is True


def test_run_survives_a_slow_import_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pathologically slow --import must not itself abort the run: the warm-up timeout is swallowed
    # and the real suite run (with its own timeout) decides. Here the import "times out" but the
    # suite writes a healthy report, so run() still returns a result.
    report = _report(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> object:
        command = args[0]
        assert isinstance(command, list)
        if "--import" in command:
            raise subprocess.TimeoutExpired(cmd="godot", timeout=1.0)
        report.write_text('<testsuite tests="2" failures="0"/>', encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    result = GdUnit4Runner().run(str(tmp_path))
    assert (result.tests, result.failed) == (2, False)


def test_prepare_retries_after_a_non_timeout_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-timeout warm-up failure (a transient OSError, a permission error, …) must NOT poison the
    # warm-up on a reused runner instance: the retryable state is only marked done once the scan
    # actually completes, so a later run() (after the transient cause clears) re-attempts the
    # import rather than silently skipping it forever. A swallowed timeout, by
    # contrast, IS deliberately marked done (don't re-attempt a slow import every mutant — see the
    # slow-import test above).
    report = _report(tmp_path)
    calls: list[list[str]] = []

    def flaky(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        calls.append(command)
        if "--import" in command:
            if len([c for c in calls if "--import" in c]) == 1:
                raise OSError("transient failure warming up")
            return
        report.write_text('<testsuite tests="1" failures="0"/>', encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", flaky)
    runner = GdUnit4Runner(godot="godot4")
    with pytest.raises(OSError, match="transient"):
        runner.run(str(tmp_path))
    runner.run(str(tmp_path))  # same instance, retried after the transient cause clears

    import_calls = [c for c in calls if "--import" in c]
    assert len(import_calls) == 2  # re-attempted, not poisoned by the first failure


def test_gdunit4_runner_satisfies_the_protocol() -> None:
    assert isinstance(GdUnit4Runner(), Runner)


def test_with_filename_leaves_an_already_named_error_alone() -> None:
    # POSIX already sets .filename on a missing-executable FileNotFoundError — don't touch it.
    # (with_filename itself now lives in engine.runner — see test_runner.py for its own unit
    # tests — this just confirms the gdscript adapter still imports and uses the shared helper.)
    error = FileNotFoundError(2, "No such file or directory", "/already/set")
    assert with_filename(error, "/attempted/godot") is error


def test_with_filename_patches_a_filename_less_error() -> None:
    # Windows' CreateProcess failure leaves .filename None (verified live) — the CLI's
    # missing-executable hint needs a name to show the user, so patch one in.
    error = FileNotFoundError(2, "The system cannot find the file specified")
    assert error.filename is None
    patched = with_filename(error, "/attempted/godot")
    assert patched.filename == "/attempted/godot"
    assert patched.errno == 2


def test_prepare_names_the_attempted_godot_path_when_the_os_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproduces the Windows CreateProcess behavior: FileNotFoundError with no .filename. Without
    # the fix, the CLI's missing-executable hint falls back to a generic placeholder
    # instead of naming the actual --godot path the user got wrong.
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    runner = GdUnit4Runner(godot="/nonexistent/godot")
    with pytest.raises(FileNotFoundError) as excinfo:
        runner.run(str(tmp_path))
    assert excinfo.value.filename == "/nonexistent/godot"


def test_run_names_the_attempted_godot_path_when_the_os_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same as above, but for the suite invocation itself (after a successful warm-up) rather than
    # the --import warm-up.
    def flaky(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        if "--import" in command:
            return
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(runner_mod.subprocess, "run", flaky)
    runner = GdUnit4Runner(godot="/nonexistent/godot")
    with pytest.raises(FileNotFoundError) as excinfo:
        runner.run(str(tmp_path))
    assert excinfo.value.filename == "/nonexistent/godot"
