"""Tests for the CLI (`gdmutant run`), driven without Godot via an injected fake runner."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import MarkerRunner

import gdmutant.cli as cli
from gdmutant.cli import build_parser, list_mutants, main, run_mutation
from gdmutant.engine.runner import SuiteResult


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
    assert data["files"][str(path)]["language"] == "gdscript"  # CLI passes the language through
    assert data["files"][str(path)]["source"] == path.read_text(encoding="utf-8")  # real source
    assert "Wrote report to" in capsys.readouterr().out  # the confirmation line is printed


def test_run_mutation_json_dash_writes_pure_json_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --json - streams the report to stdout (for agents piping it); the human summary moves to
    # stderr so stdout parses as clean JSON.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path="-")
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout is valid JSON, nothing else mixed in
    assert data["schemaVersion"] == "2"
    assert data["files"][str(path)]["language"] == "gdscript"
    assert "Mutation score:" in captured.err  # the human summary went to stderr
    assert "Mutation score:" not in captured.out


def test_run_mutation_baseline_failure_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    # marker present in the ORIGINAL source -> the unmutated baseline "fails".
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">"))
    assert rc == 1
    assert "unmutated test suite failed" in capsys.readouterr().err


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


def test_run_mutation_unparseable_gdscript_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A syntactically invalid .gd file can't be mutated — exit 2 with a clear message, not a raw
    # lark traceback (the most likely bad input for a source-mutating tool).
    path = tmp_path / "broken.gd"
    path.write_text("func broken(:\n", encoding="utf-8")
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), "x"))
    assert rc == 2
    assert "not valid GDScript" in capsys.readouterr().err


def test_run_mutation_unwritable_json_path_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mutation run succeeds, but the --json target can't be written (its parent is a file, not
    # a dir): exit 2 with a message instead of an uncaught OSError.
    path = _gd(tmp_path)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    bad_json = not_a_dir / "report.json"  # parent is a regular file -> write fails
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path=str(bad_json)
    )
    assert rc == 2
    assert "cannot write report" in capsys.readouterr().err


def test_list_mutants_prints_every_mutant_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # _gd is "func f(a, b):\n\treturn a > b and a < b\n" -> 3 mutants: >, and, <.
    path = _gd(tmp_path)
    rc = list_mutants(str(path))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("3 mutants for ")
    assert f"  {path}:2:11  comparison  > -> >=" in out  # exact loc + format for the '>' mutant
    assert "boolean  and -> or" in out
    assert "comparison  < -> <=" in out


def test_list_mutants_unreadable_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = list_mutants(str(tmp_path / "nope.gd"))
    assert rc == 2
    assert "cannot read" in capsys.readouterr().err


def test_list_mutants_unparseable_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.gd"
    path.write_text("func broken(:\n", encoding="utf-8")
    rc = list_mutants(str(path))
    assert rc == 2
    assert "not valid GDScript" in capsys.readouterr().err


def test_main_dry_run_needs_no_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run must not construct a runner or need Godot: fail loudly if it tries.
    path = _gd(tmp_path)

    def boom(**kwargs: object) -> object:
        raise AssertionError("dry-run must not construct a runner")

    monkeypatch.setattr(cli, "GdUnit4Runner", boom)
    rc = main(["run", str(path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr()
    assert "mutants for" in out.out
    assert "ignored" not in out.err  # no notice when no run-only flags are passed


def test_main_dry_run_lists_every_ignored_flag_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exact message pins each flag label, the ", " join, and the plural "are" (a bare substring
    # check would still match a wrapped/renamed label).
    path = _gd(tmp_path)
    main(
        [
            "run",
            str(path),
            "--dry-run",
            "--project",
            "p",
            "--godot",
            "g",
            "--tests",
            "t",
            "--json",
            "j",
        ]
    )
    assert capsys.readouterr().err.strip() == (
        "note: --dry-run runs no tests, so --project, --godot, --tests, --json are ignored"
    )


def test_main_dry_run_singular_message_for_one_ignored_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    main(["run", str(path), "--dry-run", "--json", "j"])
    assert capsys.readouterr().err.strip() == "note: --dry-run runs no tests, so --json is ignored"


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


def test_parser_defaults() -> None:
    # The parsed Namespace pins each argument's default (public API — a mutant that drops or nulls
    # a default is caught here).
    args = build_parser().parse_args(["run", "x.gd"])
    assert args.godot == "godot"
    assert args.tests == "res://test"
    assert args.project is None
    assert args.json_path is None
    assert args.dry_run is False


def test_parser_help_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Pin the user-facing help via the public --help output (no argparse internals). Wide COLUMNS
    # keeps help strings on one line so they appear verbatim. This is the CLI's contract, so every
    # help-string mutation is caught.
    monkeypatch.setenv("COLUMNS", "200")
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    top = capsys.readouterr().out
    # Each help string ends its line, so assert it WITH the trailing newline. That also catches a
    # mutant that wraps the string ("XX...XX") — a bare substring check would still match the
    # wrapped form, since the original is a substring of it. (The description is pinned exactly by
    # test_parser_prog_and_description.)
    assert "mutate a GDScript file and report survivors\n" in top  # the run subcommand's help
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--help"])
    run_help = capsys.readouterr().out
    for expected in (
        "the .gd file to mutate",
        "the Godot project dir (default: the source's dir)",
        "the Godot executable (default: godot)",
        "the GdUnit4 test path (default: res://test)",
        "write the Stryker JSON report here (use - for stdout)",
        "list the mutants without running any tests (no Godot needed)",
    ):
        assert f"{expected}\n" in run_help


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


def test_main_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out  # the help text, not just a silent exit 0
