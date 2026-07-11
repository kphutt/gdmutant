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


def test_run_mutation_emits_per_mutant_progress_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Per-mutant progress goes to stderr so a real run doesn't look hung; the "[i/N]"
    # counter proves one line per mutant. The default (non --json -) summary is on stdout, so the
    # progress lines must NOT be there.
    path = _gd(tmp_path)  # 3 mutants
    run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="))
    captured = capsys.readouterr()
    assert "[1/3]" in captured.err and "[3/3]" in captured.err
    assert "... killed" in captured.err
    assert "[1/3]" not in captured.out  # progress never pollutes stdout


def test_run_mutation_json_dash_keeps_progress_off_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # With --json - stdout must be pure JSON: the progress lines go to stderr alongside the summary.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path="-")
    assert rc == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout is still valid JSON, nothing mixed in
    assert "[1/3]" in captured.err  # progress landed on stderr


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


@dataclass
class MissingGodotRunner:
    """Raises FileNotFoundError on the baseline run, as subprocess does when `godot` isn't found.

    `filename=None` models the case where the OS error carries no filename (the `.filename or ...`
    fallback path)."""

    filename: str | None = "godot"

    def run(self, project_dir: str) -> SuiteResult:
        raise FileNotFoundError(2, "No such file or directory", self.filename)


# The exact messages, pinned in full (as a stderr *suffix* — the baseline-progress notice prints
# first) so mutmut's string mutation is caught: a wrapped "XX…--godot.XX\n" fails an endswith of the
# verbatim block, where a loose "--godot" in err would not.
_GENERIC_GODOT_MISSING = (
    "error: could not run the test suite — executable 'godot' not found.\n"
    "  Install it and put it on your PATH, or pass its full path with --godot.\n"
)
_MACOS_GODOT_MISSING = (
    _GENERIC_GODOT_MISSING
    + "  On macOS, Godot ships as an app bundle and is never on PATH — pass the binary directly:\n"
    + "    --godot /Applications/Godot.app/Contents/MacOS/Godot\n"
)
_FALLBACK_MISSING = (
    "error: could not run the test suite — executable 'the test runner' not found.\n"
    "  Install it and put it on your PATH, or pass its full path with --godot.\n"
)


def test_run_mutation_missing_godot_returns_two_with_exact_generic_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A not-found runner executable is a SETUP error (exit 2), not a red baseline (exit 1). Off
    # darwin the message is exactly the generic two lines — no macOS hint, no raw errno.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MissingGodotRunner())
    assert rc == 2  # setup error, distinct from baseline-red (1)
    err = capsys.readouterr().err
    assert err.endswith(_GENERIC_GODOT_MISSING)  # exact message, verbatim
    assert "Godot.app" not in err  # macOS-only hint suppressed off darwin
    assert "Errno" not in err  # the raw errno never leaks


def test_run_mutation_missing_godot_shows_exact_macos_hint_on_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # On macOS — where most Godot users are and Godot is never on PATH — the generic message is
    # followed by the exact app-bundle path to pass to --godot.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MissingGodotRunner())
    assert rc == 2
    assert capsys.readouterr().err.endswith(_MACOS_GODOT_MISSING)


def test_run_mutation_missing_non_godot_binary_omits_godot_specific_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The macOS app-bundle hint is Godot-specific: a different missing binary (even on darwin) gets
    # the generic message only, so the hint can't misfire for a non-Godot runner.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MissingGodotRunner(filename="some-runner"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "executable 'some-runner' not found." in err  # the missing name is substituted in
    assert "Godot.app" not in err


def test_run_mutation_missing_executable_with_no_filename_uses_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the OS error carries no filename, the message names "the test runner" rather than an
    # empty ''. Exercises the `.filename or "the test runner"` fallback so the literal can't rot.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MissingGodotRunner(filename=None))
    assert rc == 2
    assert capsys.readouterr().err.endswith(_FALLBACK_MISSING)


def test_baseline_red_is_still_exit_one_not_mistaken_for_missing_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A genuinely failing baseline (no FileNotFoundError cause) must stay exit 1 — the missing-exe
    # path must not swallow it. Guards the __cause__ discrimination in _missing_executable.
    path = _gd(tmp_path)
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
            "--report-path",
            "r",
            "--timeout",
            "1",
            "--json",
            "j",
        ]
    )
    # Every run-only flag, in declaration order — pins each label (--report-path/--timeout included)
    # and the ", " join, so a mutated label or a dropped flag is caught.
    assert capsys.readouterr().err.strip() == (
        "note: --dry-run runs no tests, so "
        "--project, --godot, --tests, --report-path, --timeout, --json are ignored"
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
    assert args.report_path == "reports/report_1/results.xml"
    assert args.timeout == 600.0


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
        "GdUnit4 JUnit-XML path, relative to the project dir (default: reports/report_1/results.xml)",  # noqa: E501
        "per-mutant test-run timeout, in seconds (default: 600)",
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
    # report_path/timeout are passed too — defaulted here (not on the command line).
    assert captured == {
        "test_path": "res://custom",
        "godot": "godot4",
        "report_path": "reports/report_1/results.xml",
        "timeout": 600.0,
    }


def test_main_threads_report_path_and_timeout_to_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --report-path and --timeout must reach the GdUnit4Runner (both kwargs, parsed values; the
    # timeout coerced to float). A mutant that drops either kwarg or skips the type= coercion fails.
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
            "--report-path",
            "out/custom.xml",
            "--timeout",
            "42",
        ]
    )
    assert captured["report_path"] == "out/custom.xml"
    assert captured["timeout"] == 42.0 and isinstance(captured["timeout"], float)


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
