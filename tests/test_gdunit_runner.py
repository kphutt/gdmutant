"""Tests for the GdUnit4 runner (subprocess mocked — no real Godot)."""

import subprocess
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.runner import Runner, SuiteTimeout


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "reports" / "report_1" / "results.xml"
    path.parent.mkdir(parents=True)
    return path


def test_command_construction() -> None:
    cmd = GdUnit4Runner(test_path="res://test", godot="godot4").command("/proj")
    assert cmd == [
        "godot4",
        "--headless",
        "--path",
        "/proj",
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
    # or the baseline fails to even load the tool (LOD-213). Assert the --import fires first, and
    # exactly once across repeated runs (the cache persists across mutants).
    report = _report(tmp_path)
    commands: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> None:
        command = args[0]
        assert isinstance(command, list)
        commands.append(command)
        if "--import" not in command:
            report.write_text('<testsuite tests="1" failures="0"/>', encoding="utf-8")

    monkeypatch.setattr(runner_mod.subprocess, "run", capture)
    runner = GdUnit4Runner(godot="godot4")
    runner.run(str(tmp_path))
    runner.run(str(tmp_path))  # second mutant: must NOT re-import

    import_cmds = [c for c in commands if "--import" in c]
    assert len(import_cmds) == 1, f"expected exactly one --import warm-up, got {len(import_cmds)}"
    # It runs before the first suite command, and targets the resolved project path.
    assert commands[0] == ["godot4", "--headless", "--path", str(tmp_path.resolve()), "--import"]
    assert "--import" not in commands[1]  # the suite run follows the warm-up


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


def test_gdunit4_runner_satisfies_the_protocol() -> None:
    assert isinstance(GdUnit4Runner(), Runner)
