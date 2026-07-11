"""Tests for the GdUnit4 runner (subprocess mocked — no real Godot)."""

from pathlib import Path

import pytest

import gdmutant.adapters.gdscript.runner as runner_mod
from gdmutant.adapters.gdscript.runner import GdUnit4Runner
from gdmutant.engine.runner import Runner


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
    ]


def test_run_parses_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate GdUnit4 having written a JUnit report; mock the subprocess so no Godot runs.
    report = tmp_path / "reports" / "report_1" / "results.xml"
    report.parent.mkdir(parents=True)
    report.write_text('<testsuite tests="4" failures="1" errors="0"/>', encoding="utf-8")
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: None)

    result = GdUnit4Runner().run(str(tmp_path))

    assert (result.tests, result.failures) == (4, 1)
    assert result.failed is True


def test_run_reflects_latest_report_on_repeated_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine calls run() many times against the same project. With -rc 1, GdUnit4 overwrites
    # one report dir, so each run must reflect its OWN result — never a stale earlier one.
    report = tmp_path / "reports" / "report_1" / "results.xml"
    report.parent.mkdir(parents=True)
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
    assert runner.run(str(tmp_path)).failed is False  # baseline
    assert runner.run(str(tmp_path)).failed is True  # mutant reflects the fresh report


def test_gdunit4_runner_satisfies_the_protocol() -> None:
    assert isinstance(GdUnit4Runner(), Runner)
