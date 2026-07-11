"""Tests for the CLI (`gdmutant run`), driven without Godot via an injected fake runner."""

import argparse
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
    assert str(path) in out  # each survivor line names the real source path


def test_run_mutation_writes_valid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _gd(tmp_path)
    report_file = tmp_path / "report.json"
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path=str(report_file)
    )
    assert rc == 0
    text = report_file.read_text(encoding="utf-8")
    # Pretty-printed with indent=2 exactly — catches compact (indent=None) and indent=3 mutants.
    assert '\n  "schemaVersion"' in text
    data = json.loads(text)
    assert data["schemaVersion"] == "2"
    assert str(path) in data["files"]
    assert data["files"][str(path)]["mutants"]
    assert data["files"][str(path)]["source"] == path.read_text(encoding="utf-8")  # real source
    assert "Wrote report to" in capsys.readouterr().out  # the confirmation line is printed


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


def test_parser_prog_and_description() -> None:
    parser = build_parser()
    assert parser.prog == "gdmutant"
    assert parser.description == "Mutation testing for GDScript (and, in time, other languages)."


def test_parser_run_help_defaults_and_arg_help() -> None:
    # Pin the run subcommand's help, each argument's help text, and the two defaults. This is the
    # CLI's user-facing contract, so every help/default string mutation is caught here.
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    run_help = next(c for c in sub._choices_actions if c.dest == "run")
    assert run_help.help == "mutate a GDScript file and report survivors"

    by_dest = {a.dest: a for a in sub.choices["run"]._actions}
    assert by_dest["source"].help == "the .gd file to mutate"
    assert by_dest["project"].help == "the Godot project dir (default: the source's dir)"
    assert by_dest["project"].default is None
    assert by_dest["godot"].default == "godot"
    assert by_dest["godot"].help == "the Godot executable (default: godot)"
    assert by_dest["tests"].default == "res://test"
    assert by_dest["tests"].help == "the GdUnit4 test path (default: res://test)"
    assert by_dest["json_path"].help == "write the Stryker JSON report here"


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


def test_main_constructs_runner_with_test_path_and_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # main must build the GdUnit4Runner from the parsed --tests and --godot values (both kwargs,
    # correct values) — mutants that drop a kwarg or null a value are caught by the exact dict.
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> RecordingRunner:
        captured.update(kwargs)
        return RecordingRunner()

    monkeypatch.setattr(cli, "GdUnit4Runner", fake_runner)
    main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--godot",
            "godot4",
            "--tests",
            "res://custom",
        ]
    )
    assert captured == {"test_path": "res://custom", "godot": "godot4"}


def test_main_threads_json_path_to_run_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --json must reach run_mutation as json_path; a mutant that nulls/drops it writes no report.
    path = _gd(tmp_path)
    report = tmp_path / "out.json"
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    rc = main(["run", str(path), "--project", str(tmp_path), "--json", str(report)])
    assert rc == 0
    assert report.exists()


def test_main_no_command_prints_help() -> None:
    assert main([]) == 0
