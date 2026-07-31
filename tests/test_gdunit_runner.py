"""Tests for the GdUnit4 runner (subprocess mocked — no real Godot)."""

import subprocess
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.runner import Runner, SuiteTimeout, with_filename


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "reports" / "report_1" / "results.xml"
    path.parent.mkdir(parents=True)
    return path


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
    # be diagnosed instead of vanishing.
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "SCRIPT ERROR: boom"),
    )
    with pytest.raises(RuntimeError, match="SCRIPT ERROR: boom"):
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
