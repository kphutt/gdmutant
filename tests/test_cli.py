"""Tests for the CLI (`gdmutant run`), driven without Godot via an injected fake runner."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import gdmutant.cli as cli
from gdmutant.cli import build_parser, main, run_mutation
from gdmutant.engine.runner import SuiteResult


@dataclass
class MarkerRunner:
    """A fake suite that fails iff the target file contains `kill_marker`."""

    target: str
    kill_marker: str

    def run(self, project_dir: str) -> SuiteResult:
        content = Path(self.target).read_text(encoding="utf-8")
        return SuiteResult(tests=3, failures=int(self.kill_marker in content), errors=0)


def _gd(tmp_path: Path) -> Path:
    path = tmp_path / "f.gd"
    path.write_text("func f(a, b):\n\treturn a > b and a < b\n", encoding="utf-8")
    return path


def test_run_mutation_prints_summary_and_returns_zero_with_survivors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="))
    assert rc == 0  # survivors do not fail the run (FG-6.2)
    out = capsys.readouterr().out
    assert "Mutation score:" in out
    assert "Survivors" in out


def test_run_mutation_writes_valid_json(tmp_path: Path) -> None:
    path = _gd(tmp_path)
    report_file = tmp_path / "report.json"
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path=str(report_file)
    )
    assert rc == 0
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == "2"
    assert str(path) in data["files"]
    assert data["files"][str(path)]["mutants"]


def test_run_mutation_baseline_failure_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    # marker present in the ORIGINAL source -> the unmutated baseline "fails".
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">"))
    assert rc == 1
    assert "error" in capsys.readouterr().err.lower()


def test_run_mutation_missing_file_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run_mutation(str(tmp_path / "nope.gd"), str(tmp_path), MarkerRunner("x", "y"))
    assert rc == 2
    assert "cannot read" in capsys.readouterr().err


def test_run_mutation_invalid_utf8_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.gd"
    path.write_bytes(b"\xff\xfe not valid utf-8")
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), "x"))
    assert rc == 2  # UnicodeDecodeError is handled, not a crash
    assert "cannot read" in capsys.readouterr().err


def test_parser_run_subcommand() -> None:
    args = build_parser().parse_args(
        ["run", "f.gd", "--godot", "godot4", "--tests", "res://t", "--json", "r.json"]
    )
    assert args.command == "run"
    assert args.source == "f.gd"
    assert args.godot == "godot4"
    assert args.tests == "res://t"
    assert args.json_path == "r.json"


def test_main_dispatches_run_with_injected_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    # Replace the real GdUnit4Runner so no Godot is needed.
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    rc = main(["run", str(path), "--project", str(tmp_path)])
    assert rc == 0
    assert "Mutation score:" in capsys.readouterr().out


@dataclass
class RecordingRunner:
    """Records the project_dir it was called with (all-pass, so mutants survive)."""

    seen: list[str] = field(default_factory=list)

    def run(self, project_dir: str) -> SuiteResult:
        self.seen.append(project_dir)
        return SuiteResult(tests=3, failures=0, errors=0)


def test_main_default_project_uses_the_source_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gd(tmp_path)
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: runner)
    assert main(["run", str(path)]) == 0  # no --project given
    assert runner.seen[0] == str(path.resolve().parent)  # defaulted to the source's directory


def test_main_no_command_prints_help() -> None:
    assert main([]) == 0
