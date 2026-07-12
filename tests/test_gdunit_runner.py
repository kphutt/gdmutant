"""Tests for the GdUnit4 runner (subprocess mocked — no real Godot)."""

import subprocess
from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.runner import Runner


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

    (args, kwargs) = calls[0]
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


def test_gdunit4_runner_satisfies_the_protocol() -> None:
    assert isinstance(GdUnit4Runner(), Runner)
