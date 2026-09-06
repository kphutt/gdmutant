"""Tests for the CLI (`gdmutant run`), driven without Godot via an injected fake runner."""

import json
import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from conftest import MarkerRunner

import gdmutant.cli as cli
from gdmutant.adapters.gdscript.runner import GutRunner
from gdmutant.cli import (
    _CONFIG_FILENAME,
    _CONFIG_KEY_TO_DEST,
    _DEFAULT_REPORT,
    _EXAMPLE_NAME,
    _GDUNIT_ADDON_REL,
    _GUT_ADDON_REL,
    _cpu_worker_ceiling,
    _default_report_stem,
    _detect_runner,
    _git_backup,
    _load_config,
    _report_path_problem,
    _resolve_default_report_paths,
    _resolve_jobs,
    _resolve_progress_style,
    _write_example,
    _write_init_config,
    build_parser,
    list_mutants,
    main,
    run_mutation,
)
from gdmutant.engine.loop import ProgressStyle
from gdmutant.engine.runner import CommandRunner, SuiteResult

# git exports these to point subprocesses at the *invoking* repo. When pytest runs inside a git
# hook (e.g. pre-push), inheriting them makes `_git` operate on the hook's repo instead of the
# intended tmp dir — corrupting every fixture that builds a throwaway repo.
_GIT_ENV_LEAKS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, failing the test loudly on error.

    Scrubs the inherited GIT_* location vars (see `_GIT_ENV_LEAKS`) so the command acts on `repo`
    regardless of any hook environment that spawned pytest.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_LEAKS}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)


def _leak_decoy_env(decoy_repo: Path) -> dict[str, str]:
    """GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE pointing at `decoy_repo` — the shape git itself sets in
    a hook's environment, but aimed at some *other* repo than the one a test is exercising. Used by
    the regression tests below to simulate gdmutant being invoked from inside a git hook."""
    return {
        "GIT_DIR": str(decoy_repo / ".git"),
        "GIT_WORK_TREE": str(decoy_repo),
        "GIT_INDEX_FILE": str(decoy_repo / ".git" / "index"),
    }


def _init_decoy_repo(decoy_repo: Path) -> None:
    """A throwaway committed repo at `decoy_repo`, unrelated to any test's real target repo — the
    'hook's own repo' a leaked GIT_DIR/etc. would incorrectly redirect git calls to."""
    decoy_repo.mkdir()
    _git(decoy_repo, "init")
    _git(decoy_repo, "config", "user.email", "hook@example.com")
    _git(decoy_repo, "config", "user.name", "Hook")
    (decoy_repo / "unrelated.txt").write_text("x", encoding="utf-8")
    _git(decoy_repo, "add", "unrelated.txt")
    _git(decoy_repo, "commit", "-m", "decoy init")


def _committed_repo(tmp_path: Path) -> Path:
    """A git repo containing a committed, clean f.gd — the base for the dirty-tree tests."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _gd(tmp_path)  # writes f.gd
    _git(tmp_path, "add", "f.gd")
    _git(tmp_path, "commit", "-m", "add f.gd")
    return tmp_path


def _gd(tmp_path: Path) -> Path:
    path = tmp_path / "f.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a > b and a < b\n", encoding="utf-8")
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


def test_all_survived_warning_reaches_stderr_when_no_mutant_is_killed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A suite that always passes (a marker that never appears in the source) mimics tests that never
    # touch the file: every mutant survives, so the warning fires on stderr — score/exit unchanged.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), "NEVER_IN_SOURCE"))
    assert rc == 0  # a warning, not an error
    captured = capsys.readouterr()
    assert "evaluated mutants survived" in captured.err  # warning is on stderr
    assert str(path) in captured.err  # names the mutated file
    assert "Mutation score:" in captured.out  # score still printed (unchanged)


def test_all_survived_warning_absent_when_a_mutant_is_killed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A suite that catches the `>` -> `>=` mutant means a test does reach the file: not the vacuous
    # case, so no warning.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="))
    assert rc == 0
    assert "evaluated mutants survived" not in capsys.readouterr().err


class _RunWarningRunner:
    """A minimal runner that implements the optional `RunWarning` contract, for the helper tests."""

    def __init__(self, warning: str | None) -> None:
        self._warning = warning

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise NotImplementedError  # the helper never runs it; it only reads run_warning()

    def run_warning(self) -> str | None:
        return self._warning


def test_emit_runner_warning_prints_a_runners_warning_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A runner that implements RunWarning and has something to say (e.g. GUT's non-determinism
    # canary): the helper prints it on stderr, the same surface as the all-survived warning.
    cli._emit_runner_warning(_RunWarningRunner("warning: collection was non-deterministic"))
    captured = capsys.readouterr()
    assert "non-deterministic" in captured.err
    assert captured.out == ""  # never on stdout (keeps --json - clean)


def test_emit_runner_warning_silent_when_the_runner_has_nothing_to_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A RunWarning runner whose canary never fired returns None -> the helper prints nothing.
    cli._emit_runner_warning(_RunWarningRunner(None))
    assert capsys.readouterr() == ("", "")


def test_emit_runner_warning_silent_for_a_runner_without_the_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A runner that doesn't implement RunWarning (e.g. the marker runner / GdUnit4) is skipped
    # entirely via the isinstance guard — no attribute error, no output.
    cli._emit_runner_warning(MarkerRunner(str(_gd(tmp_path)), ">="))
    assert capsys.readouterr() == ("", "")


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
    # Report `files` keys are POSIX-normalized regardless of host OS, so compare against
    # `.as_posix()`, never a raw `str(Path(...))` (which renders backslashes on Windows — see
    # AGENTS.md's platform-path-rendering warning).
    key = path.as_posix()
    assert key in data["files"]
    assert data["files"][key]["mutants"]
    assert data["files"][key]["language"] == "gdscript"  # CLI passes the language through
    assert data["files"][key]["source"] == path.read_text(encoding="utf-8")  # real source
    assert "Wrote report to" in capsys.readouterr().out  # the confirmation line is printed


def test_run_mutation_writes_html_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --html writes a ready-to-open page that needs no network to render.
    path = _gd(tmp_path)
    html_file = tmp_path / "report.html"
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), html_path=str(html_file)
    )
    assert rc == 0
    html = html_file.read_text(encoding="utf-8")
    assert "<html" in html and "mutation score" in html
    assert 'src="http' not in html
    assert "Wrote HTML report to" in capsys.readouterr().out


def test_write_reports_confirmations_print_the_resolved_absolute_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The confirmation lines must name where the report actually landed, not echo back
    # whatever (possibly relative) string the caller passed in — the process's working directory is
    # not necessarily the project dir or the mutated file's own dir (the three commonly differ in a
    # real run), so a bare filename alone doesn't locate the file. Built from relpath rather than
    # os.chdir(): a global cwd change breaks mutmut's mutated-module resolution (see
    # tests/test_mutation_baseline_inputs.py), so this stays chdir-free like every other test here.
    #
    # `tmp_path` is deliberately not used: `os.path.relpath` defaults its `start` to the cwd and
    # raises `ValueError` when that cwd is on a different drive than the target, which is exactly
    # how a hosted Windows runner's checkout (`D:`) and `tmp_path` (`%TEMP%`, `C:`) commonly land —
    # see `test_an_ordinary_file_is_named_once_and_only_once` for the same trap. A scratch dir made
    # with `tempfile.TemporaryDirectory(dir=".")` sits inside the checkout itself, same drive as the
    # cwd by construction.
    with tempfile.TemporaryDirectory(dir=".") as scratch:
        json_rel = os.path.relpath(str(Path(scratch) / "r.json"))
        html_rel = os.path.relpath(str(Path(scratch) / "r.html"))
        rc = cli._write_reports({"schemaVersion": "2", "files": {}}, json_rel, html_rel, scratch)
        assert rc == 0
        out = capsys.readouterr().out
        assert os.path.abspath(json_rel) in out
        assert os.path.abspath(html_rel) in out
        # The relative strings themselves are gone, not just overshadowed by the absolute ones.
        assert json_rel not in out.replace(os.path.abspath(json_rel), "")
        assert html_rel not in out.replace(os.path.abspath(html_rel), "")


def test_write_reports_write_errors_still_echo_the_literal_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A write failure must keep quoting the path exactly as the caller typed it, so a mistyped path
    # is visible as a typo rather than resolved into something that looks like a real location.
    bad_json = str(tmp_path / "missing-dir" / "r.json")
    rc = cli._write_reports({"schemaVersion": "2", "files": {}}, bad_json, None, str(tmp_path))
    assert rc == 2
    assert bad_json in capsys.readouterr().err

    bad_html = str(tmp_path / "missing-dir" / "r.html")
    rc = cli._write_reports({"schemaVersion": "2", "files": {}}, None, bad_html, str(tmp_path))
    assert rc == 2
    assert bad_html in capsys.readouterr().err


def test_run_mutation_shows_paths_relative_to_the_project_in_the_html_report(
    tmp_path: Path,
) -> None:
    # The project root only exists at this level, so this is the seam where it has to be handed
    # over. A report is made to travel; without this the page carries the author's username and
    # directory layout in every row of the one column a reader scans.
    source = tmp_path / "src" / "f.gd"
    source.parent.mkdir()
    source.write_text("func f(a, b) -> bool:\n\treturn a > b and a < b\n", encoding="utf-8")
    html_file = tmp_path / "report.html"
    json_file = tmp_path / "report.json"
    rc = run_mutation(
        str(source),
        str(tmp_path),
        MarkerRunner(str(source), ">="),
        html_path=str(html_file),
        json_path=str(json_file),
    )
    assert rc == 0
    page = html_file.read_text(encoding="utf-8")
    assert '"path": "src/f.gd"' in page
    # Nothing a reader sees carries the absolute path.
    visible = page.split('<script type="application/json"', 1)[0]
    assert str(tmp_path).replace("\\", "/") not in visible
    # The JSON report's keys are POSIX-normalized, regardless of host OS.
    assert source.as_posix() in json.loads(json_file.read_text(encoding="utf-8"))["files"]


def test_run_mutation_emits_progress_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Progress (the pre-run plan + the closing wall-clock) goes to stderr so a real run doesn't
    # look hung. The default (non --json -) summary is on stdout, so progress must NOT be there.
    path = _gd(tmp_path)  # 3 mutants
    run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="))
    captured = capsys.readouterr()
    assert "mutants to run" in captured.err and "Done in" in captured.err
    assert "mutants to run" not in captured.out  # progress never pollutes stdout


def test_run_mutation_json_dash_keeps_progress_off_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # With --json - stdout must be pure JSON: the progress lines go to stderr alongside the summary.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path="-")
    assert rc == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout is still valid JSON, nothing mixed in
    assert "mutants to run" in captured.err  # progress landed on stderr


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
    assert data["files"][path.as_posix()]["language"] == "gdscript"  # POSIX-keyed regardless of OS
    assert "Mutation score:" in captured.err  # the human summary went to stderr
    assert "Mutation score:" not in captured.out


def test_json_dash_with_html_leaves_stdout_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The defect this pins: `--json -` streams the report to stdout, and `--html` used to append
    # "Wrote HTML report to ..." there too. A caller piping the run into json.loads got a parse
    # error pointing at a column of trailing prose, with nothing to say that --html had caused it.
    # The note is human text, so with `--json -` it belongs on stderr beside the summary.
    path = _gd(tmp_path)
    html_file = tmp_path / "report.html"
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), ">="),
        json_path="-",
        html_path=str(html_file),
    )
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # the whole of stdout parses — nothing trails the JSON
    assert data["schemaVersion"] == "2"
    assert "Wrote HTML report to" not in captured.out
    assert "Wrote HTML report to" in captured.err  # the user still learns where the page went
    assert "<html" in html_file.read_text(encoding="utf-8")  # ... and it really was written


def test_the_html_note_stays_on_stdout_when_the_report_goes_to_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The stderr routing above is *only* for `--json -`. With the report in a file, stdout is the
    # human channel, and moving the confirmations off it wholesale would hide them from the person
    # who ran the command.
    path = _gd(tmp_path)
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), ">="),
        json_path=str(tmp_path / "report.json"),
        html_path=str(tmp_path / "report.html"),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Wrote report to" in captured.out
    assert "Wrote HTML report to" in captured.out
    assert "Wrote HTML report to" not in captured.err


# --- --json - and --report step-summary: two documents, one stdout ----------------------------
#
# The `--html` note above is human text and simply moves to stderr. The step-summary Markdown is a
# *document*: its stdout fallback exists so `> summary.md` works locally. There is no destination
# that keeps both it and the JSON readable, so the combination is refused up front instead.


def test_json_dash_with_step_summary_and_no_env_var_is_refused_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _gd(tmp_path)
    runner = RecordingRunner()
    rc = run_mutation(str(path), str(tmp_path), runner, json_path="-", step_summary=True)
    assert rc == 2
    captured = capsys.readouterr()
    assert "--json -" in captured.err and "--report step-summary" in captured.err
    assert "GITHUB_STEP_SUMMARY" in captured.err  # the message names the way out
    assert captured.out == ""  # nothing half-written to the stream the caller is parsing
    assert runner.seen == []  # refused up front, not after minutes of booting Godot


def test_json_dash_with_step_summary_is_allowed_once_the_env_var_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other side, and the one that matters: the shipped GitHub Action pairs these two flags on
    # every run, and Actions always sets the variable. The Markdown has a file to go to, so stdout
    # stays the report's alone and both work together.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = _gd(tmp_path)
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), "NEVER_IN_SOURCE"),  # everything survives, so there is Markdown
        json_path="-",
        step_summary=True,
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schemaVersion"] == "2"
    assert "Surviving mutants" in summary.read_text(encoding="utf-8")


def test_step_summary_still_falls_back_to_stdout_without_json_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The refusal is scoped to the collision. With no report on stdout to collide with, the local
    # fallback is untouched — `gdmutant run f.gd --report step-summary > summary.md` still works.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _gd(tmp_path)
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), "NEVER_IN_SOURCE"),
        step_summary=True,
    )
    assert rc == 0
    assert "Surviving mutants" in capsys.readouterr().out


def test_step_summary_with_a_json_file_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--json <path>` is not `--json -`: the report goes to the file, so stdout is free for the
    # Markdown and nothing collides.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _gd(tmp_path)
    report = tmp_path / "r.json"
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), "NEVER_IN_SOURCE"),
        json_path=str(report),
        step_summary=True,
    )
    assert rc == 0
    assert "Surviving mutants" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["schemaVersion"] == "2"


def test_json_dash_without_step_summary_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ... and the reporter has to actually be asked for. `--json -` alone is the ordinary agent
    # invocation and must never trip the refusal.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path="-")
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["schemaVersion"] == "2"


def test_the_step_summary_collision_is_refused_on_the_multi_file_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # run_mutation_paths preflights separately, so a guard added to only one of the two entry points
    # would pass every single-file test above.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    first = _gd(tmp_path)
    second = tmp_path / "g.gd"
    second.write_text("func g(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    runner = RecordingRunner()
    rc = cli.run_mutation_paths(
        [str(first), str(second)], str(tmp_path), runner, json_path="-", step_summary=True
    )
    assert rc == 2
    assert "--report step-summary" in capsys.readouterr().err
    assert runner.seen == []


def test_main_refuses_the_step_summary_collision_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Through main(), which is where a real caller meets it: the flags as typed, and exit 2.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _gd(tmp_path)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    rc = main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--runner",
            "gdunit4",
            "--json",
            "-",
            "--report",
            "step-summary",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "--json -" in captured.err and "--report step-summary" in captured.err
    assert captured.out == ""


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

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise FileNotFoundError(2, "No such file or directory", self.filename)


# The exact messages, pinned in full (as a stderr *suffix* — the baseline-progress notice prints
# first) so mutmut's string mutation is caught: a wrapped "XX…--godot.XX\n" fails an endswith of the
# verbatim block, where a loose "--godot" in err would not.
_GENERIC_GODOT_MISSING = (
    "error: could not run the test suite: executable 'godot' not found.\n"
    "  Install it and put it on your PATH, or pass its full path with --godot.\n"
)
_MACOS_GODOT_MISSING = (
    _GENERIC_GODOT_MISSING
    + "  On macOS, Godot ships as an app bundle and is never on PATH. Pass the binary directly:\n"
    + "    --godot /Applications/Godot.app/Contents/MacOS/Godot\n"
)
_FALLBACK_MISSING = (
    "error: could not run the test suite: executable 'the test runner' not found.\n"
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


# --- the missing-executable hint under --runner command ---------------------------------------
#
# The exit-code runner takes its executable from --command, not --godot. The generic hint's advice
# ("pass its full path with --godot") is therefore *wrong* in that mode: a user who follows it gets
# the byte-identical error back and has to go read the README to get unstuck. These pin the
# mode-aware branch, each message in full so a mutated string is caught.

#: A command whose first token cannot exist on any platform, so the real `CommandRunner` raises the
#: real `FileNotFoundError` a missing `godot` would — no mocking of the failure under test.
_ABSENT = "gdmutant-no-such-binary"
_COMMAND_MODE_HINT = (
    "  With --runner command the executable comes from the --command string itself. --godot\n"
    "  is not read in this mode, so setting it changes nothing. Put the full path inside\n"
    "  --command instead, quoted if it contains spaces:\n"
)


def test_command_runner_missing_executable_names_command_not_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The whole defect in one assertion: under --runner command the message must NOT tell the user
    # to pass --godot (they will, and nothing will change), and it must show their own command back
    # with the executable slot marked.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    path = _gd(tmp_path)
    runner = CommandRunner(command=[_ABSENT, "--headless", "--script", "res://tests/run.gd"])
    rc = run_mutation(str(path), str(tmp_path), runner)
    assert rc == 2  # still a setup error, not a red baseline
    err = capsys.readouterr().err
    assert err.endswith(
        f"error: could not run the test suite: executable '{_ABSENT}' not found.\n"
        + _COMMAND_MODE_HINT
        + f'    --command "<full path to {_ABSENT}> --headless --script res://tests/run.gd"\n'
    )
    assert "pass its full path with --godot" not in err  # the advice that does not work
    assert "Godot.app" not in err  # macOS-only, and this is linux


def test_command_runner_hint_omits_arguments_for_a_bare_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A one-token --command has no arguments to echo back, so the example is the executable alone —
    # never a dangling "<full path to x> " with a trailing space inside the quotes.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), CommandRunner(command=[_ABSENT]))
    assert rc == 2
    assert capsys.readouterr().err.endswith(f'    --command "<full path to {_ABSENT}>"\n')


def test_command_runner_missing_godot_on_darwin_gives_the_bundle_path_not_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # macOS users need the app-bundle path either way — but phrased for the flag this mode reads.
    # Repeating "--godot /Applications/..." here would re-make the very mistake being corrected.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    path = _gd(tmp_path)
    runner = CommandRunner(command=["godot-absent-for-this-test", "--headless"])
    rc = run_mutation(str(path), str(tmp_path), runner)
    assert rc == 2
    err = capsys.readouterr().err
    assert err.endswith(
        "  On macOS, Godot ships as an app bundle and is never on PATH. Its binary is at:\n"
        "    /Applications/Godot.app/Contents/MacOS/Godot\n"
    )
    assert "--godot /Applications" not in err


def test_command_runner_non_godot_binary_on_darwin_omits_the_bundle_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The bundle hint is Godot-specific in command mode too: a hand-rolled harness that isn't Godot
    # must not be told where Godot lives.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), CommandRunner(command=["run-my-suite-absent"]))
    assert rc == 2
    assert "Godot.app" not in capsys.readouterr().err


def test_junit_runner_keeps_the_godot_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of "mode-aware": for a runner that DOES read --godot, the original advice is
    # correct and stays. `_command_argv` returns None for anything that isn't a CommandRunner.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), MissingGodotRunner())
    assert rc == 2
    err = capsys.readouterr().err
    assert err.endswith(_GENERIC_GODOT_MISSING)
    assert "--runner command" not in err


# --- the cold-import notice --------------------------------------------------------------------
#
# On a checkout Godot has never opened, importing every asset precedes any test run — minutes of
# silence that reads as a hang. The JUnit runners pay it inside `prepare` and the engine announces
# it; --runner command has no such hook, so it says so instead.


def _import_notice_args(tmp_path: Path, path: Path) -> list[str]:
    return [
        "run",
        str(path),
        "--project",
        str(tmp_path),
        "--runner",
        "command",
        "--command",
        "run-the-suite",
    ]


def test_command_runner_warns_when_the_project_was_never_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)  # no .godot/ in tmp_path — an un-imported checkout
    monkeypatch.setattr(cli, "CommandRunner", lambda **kwargs: RecordingRunner())
    assert main(_import_notice_args(tmp_path, path)) == 0
    err = capsys.readouterr().err
    assert f"note: {tmp_path} has no Godot import cache (.godot/ is not there)" in err
    # The fix is a command the reader can paste, aimed at their own project.
    assert f"    godot --headless --path {tmp_path} --import" in err


def test_command_runner_silent_when_the_project_has_an_import_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reads one fact — does `.godot/` exist — so a project that is merely slow is never accused of
    # anything, and an already-imported project gets no noise.
    path = _gd(tmp_path)
    (tmp_path / ".godot").mkdir()
    monkeypatch.setattr(cli, "CommandRunner", lambda **kwargs: RecordingRunner())
    assert main(_import_notice_args(tmp_path, path)) == 0
    assert "import cache" not in capsys.readouterr().err


def test_junit_runner_does_not_print_the_cold_import_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # gdunit4/gut warm the cache themselves, so the notice would be telling the user to do work the
    # tool is about to do — it is scoped to the one mode that cannot.
    path = _gd(tmp_path)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    assert main(["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4"]) == 0
    assert "import cache" not in capsys.readouterr().err


def test_jobs_must_be_a_positive_integer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --jobs < 1 is a setup error (exit 2), rejected up front before any run starts.
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--jobs", "0"])
    assert rc == 2
    assert "--jobs must be a positive integer" in capsys.readouterr().err


def test_a_real_run_with_no_runner_set_anywhere_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No --runner flag, and no .gdmutant.toml supplying one either: a real run must refuse rather
    # than silently pick one for the caller (there is no `default=` on --runner — see build_parser).
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--project", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--runner is required" in err
    assert "gdunit4, gut, or command" in err


def test_dry_run_needs_no_runner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --dry-run never runs a test suite, so it never needs to know which runner would have been
    # used — unlike a real run, it must not refuse just because --runner was never set.
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--dry-run"])
    assert rc == 0
    assert "--runner is required" not in capsys.readouterr().err


def test_a_config_supplied_runner_satisfies_the_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project's own .gdmutant.toml persisting `runner` once counts as having said which one —
    # only a run with genuinely nothing set anywhere (no flag, no config) is refused.
    path = _gd(tmp_path)
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('runner = "gut"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GutRunner", lambda **kwargs: runner)
    assert main(["run", str(path), "--project", str(tmp_path)]) == 0


def test_jobs_rejects_a_non_numeric_non_auto_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--jobs", "nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--jobs must be a positive integer" in err and "'auto'" in err


def test_resolve_jobs_parses_auto_explicit_and_invalid_values() -> None:
    assert _resolve_jobs("auto") == (_cpu_worker_ceiling(), True)
    assert _resolve_jobs("4") == (4, False)
    assert _resolve_jobs("1") == (1, False)
    assert _resolve_jobs("0") == (None, False)  # not positive
    assert _resolve_jobs("-1") == (None, False)  # not positive
    assert _resolve_jobs("nope") == (None, False)  # not an integer, not 'auto'


def test_cpu_worker_ceiling_matches_os_cpu_count_or_four(monkeypatch: pytest.MonkeyPatch) -> None:
    # mutmut's own default, so a machine with no reported CPU count still gets a sane ceiling.
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert _cpu_worker_ceiling() == 4
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert _cpu_worker_ceiling() == 8


def test_main_jobs_auto_resolves_to_the_cpu_ceiling_with_throttling_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No chdir, no real Godot run: capture what main() hands run_mutation, the same pattern
    # test_main_bare_json_and_html_resolve_to_paired_timestamped_paths uses for --json/--html.
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    rc = main(
        ["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4", "--jobs", "auto"]
    )
    assert rc == 0
    assert captured["jobs"] == 6
    assert captured["jobs_auto"] is True


def test_main_explicit_jobs_never_sets_the_auto_throttle_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    rc = main(["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4", "--jobs", "3"])
    assert rc == 0
    assert captured["jobs"] == 3
    assert captured["jobs_auto"] is False


def test_run_mutation_nonexistent_project_dir_reports_directory_not_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A bad --project must report a *directory* problem, not be mistaken for a missing godot binary
    # (both surface as FileNotFoundError once the runner shells out with cwd=project_dir). The
    # MissingGodotRunner would raise the executable error if reached — so this also proves the
    # project-dir check fires *before* the runner runs.
    path = _gd(tmp_path)
    missing_dir = tmp_path / "no-such-project"
    rc = run_mutation(str(path), str(missing_dir), MissingGodotRunner())
    assert rc == 2
    err = capsys.readouterr().err
    assert err == f"error: project directory not found: {missing_dir}\n"
    assert "executable" not in err  # never misreported as a missing binary


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
    # The mutation run succeeds, but the --json target can't be written (its parent is a file, not a
    # dir): exit 2 with a message instead of an uncaught OSError. This is the late-write backstop —
    # the parent *exists and is writable as a file*, so the preflight (which checks existence +
    # writability of the parent) lets it through, and the write itself fails.
    path = _gd(tmp_path)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    bad_json = not_a_dir / "report.json"  # parent is a regular file -> write fails
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), json_path=str(bad_json)
    )
    assert rc == 2
    assert "cannot write report" in capsys.readouterr().err


def test_run_mutation_unwritable_html_path_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same late-write backstop for --html: parent is a regular file (passes the preflight), so the
    # write itself fails -> exit 2 with a clear message, not an uncaught OSError.
    path = _gd(tmp_path)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    bad_html = not_a_dir / "report.html"  # parent is a regular file -> write fails
    rc = run_mutation(
        str(path), str(tmp_path), MarkerRunner(str(path), ">="), html_path=str(bad_html)
    )
    assert rc == 2
    assert "cannot write HTML report" in capsys.readouterr().err


def test_report_path_problem_accepts_stdout_none_and_a_writable_dir(tmp_path: Path) -> None:
    assert _report_path_problem(None, "--json", stdout_ok=True) is None
    assert _report_path_problem("-", "--json", stdout_ok=True) is None  # stdout, allowed for --json
    ok = _report_path_problem(str(tmp_path / "report.json"), "--json", stdout_ok=True)
    assert ok is None  # tmp_path is a writable dir


def test_report_path_problem_rejects_stdout_when_not_supported() -> None:
    # --html has no stdout target; '-' must be rejected (named for --html), not written as file '-'.
    problem = _report_path_problem("-", "--html", stdout_ok=False)
    assert problem is not None and "--html" in problem and "stdout" in problem


def test_report_path_problem_flags_a_missing_directory_naming_the_flag(tmp_path: Path) -> None:
    problem = _report_path_problem(
        str(tmp_path / "nope" / "report.html"), "--html", stdout_ok=False
    )
    assert problem is not None and "does not exist" in problem and "--html" in problem  # right flag


def test_report_path_problem_flags_an_unwritable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an existing-but-unwritable parent (chmod is unreliable under root, e.g. in CI).
    monkeypatch.setattr(cli.os, "access", lambda *_a, **_k: False)
    problem = _report_path_problem(str(tmp_path / "report.json"), "--json", stdout_ok=True)
    assert problem is not None and "not writable" in problem


class RaiseIfRunRunner:
    """A runner whose .run() fails the test if ever called — proves a preflight fired *before* any
    run started."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise AssertionError("runner.run must not be called when the report path is bad")


def test_run_mutation_preflights_a_bad_report_dir_before_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A --json target in a nonexistent directory must fail *before* booting Godot per mutant
    # not after a multi-minute run — so the runner is never invoked.
    path = _gd(tmp_path)
    bad_json = tmp_path / "missing" / "report.json"
    rc = run_mutation(str(path), str(tmp_path), RaiseIfRunRunner(), json_path=str(bad_json))
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_run_mutation_preflights_a_bad_html_dir_before_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The --html target is preflighted the same way as --json — a bad dir fails before any run.
    path = _gd(tmp_path)
    bad_html = tmp_path / "missing" / "report.html"
    rc = run_mutation(str(path), str(tmp_path), RaiseIfRunRunner(), html_path=str(bad_html))
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


class NoReportRunner:
    """Raises GdUnit4Runner's exact 'wrote no report' RuntimeError on the baseline — what an
    uninstalled/broken GdUnit4 addon produces (as opposed to a missing godot binary)."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise RuntimeError("GdUnit4 wrote no report at res://reports. Godot may have failed to run")


def test_run_mutation_missing_addon_returns_two_with_actionable_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A GdUnit4 baseline that wrote no report AND no addon installed is an addon-absent *setup*
    # error (exit 2) — surface an actionable hint, not a raw stderr dump. No addon here.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), NoReportRunner())
    assert rc == 2
    err = capsys.readouterr().err
    assert "GdUnit4 addon was not found" in err and "--runner command" in err


def test_run_mutation_no_report_with_addon_present_is_generic_baseline_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same 'wrote no report' error, but the addon IS installed -> not a setup problem: fall through
    # to the generic exit 1 with the raw error, never the misleading addon hint.
    path = _gd(tmp_path)
    (tmp_path / "addons" / "gdUnit4").mkdir(parents=True)
    rc = run_mutation(str(path), str(tmp_path), NoReportRunner())
    assert rc == 1
    err = capsys.readouterr().err
    assert "GdUnit4 addon was not found" not in err
    assert "wrote no report" in err  # the raw runner error is still surfaced


class GutNoReportRunner:
    """Raises GutRunner's exact 'wrote no report' RuntimeError on the baseline — what an
    uninstalled/broken GUT addon produces (Godot can't load gut_cmdln.gd, so nothing is written)."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise RuntimeError("GUT wrote no report at res://reports. Godot may have failed to run")


def test_run_mutation_missing_gut_addon_returns_two_with_actionable_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A GUT baseline that wrote no report AND no GUT addon installed is an addon-absent setup error
    # (exit 2) — the GUT-specific hint fires (gated on GUT's own message signature, so it never
    # cross-fires with the GdUnit4 hint). No addon here.
    path = _gd(tmp_path)
    rc = run_mutation(str(path), str(tmp_path), GutNoReportRunner())
    assert rc == 2
    err = capsys.readouterr().err
    assert "GUT addon was not found" in err and "--runner command" in err
    assert "GdUnit4 addon was not found" not in err  # the GdUnit4 hint must not misfire


def test_run_mutation_gut_no_report_with_addon_present_is_generic_baseline_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same GUT 'wrote no report' error, but the GUT addon IS installed -> not a setup problem: fall
    # through to the generic exit 1 with the raw error, never the misleading addon hint.
    path = _gd(tmp_path)
    (tmp_path / "addons" / "gut").mkdir(parents=True)
    rc = run_mutation(str(path), str(tmp_path), GutNoReportRunner())
    assert rc == 1
    err = capsys.readouterr().err
    assert "GUT addon was not found" not in err
    assert "wrote no report" in err  # the raw runner error is still surfaced


def test_list_mutants_prints_every_mutant_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # _gd is "func f(a, b) -> bool:\n\treturn a > b and a < b\n" -> 3 mutants: >, and, <.
    path = _gd(tmp_path)
    rc = list_mutants(str(path))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("3 mutants for ")
    # The location is POSIX-normalized (forward slashes) regardless of host OS — never
    # `f"{path}"`, which renders backslashes on Windows (AGENTS.md's platform-path-rendering trap).
    assert f"  {path.as_posix()}:2:11  comparison  > -> >=" in out  # exact loc for the '>' mutant
    assert "boolean  and -> or" in out
    assert "comparison  < -> <=" in out


def test_list_mutants_header_and_locations_agree_on_posix_separators(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # On Windows, the header used to print the raw (often forward-slash, as-typed) source path
    # while every per-mutant line printed `str(Path(source_path))` (the OS separator) — one
    # console listing, disagreeing with itself. Both must now render the same POSIX path, and a
    # leading "./" (pathlib's own normal form) must vanish from both alike. Built from relpath
    # rather than os.chdir(): a global cwd change breaks mutmut's mutated-module resolution (see
    # tests/test_mutation_baseline_inputs.py), so this stays chdir-free like every other test here.
    #
    # `tmp_path` is deliberately not used: `os.path.relpath` defaults its `start` to the cwd and
    # raises `ValueError` when that cwd is on a different drive than the target, which is exactly
    # how a hosted Windows runner's checkout (`D:`) and `tmp_path` (`%TEMP%`, `C:`) commonly land —
    # see `test_an_ordinary_file_is_named_once_and_only_once` for the same trap. A scratch dir made
    # with `tempfile.TemporaryDirectory(dir=".")` sits inside the checkout itself, same drive as the
    # cwd by construction.
    with tempfile.TemporaryDirectory(dir=".") as scratch:
        path = _gd(Path(scratch))
        rel = Path(os.path.relpath(str(path))).as_posix()
        dotted = f"./{rel}"
        rc = list_mutants(dotted)
        assert rc == 0
        out = capsys.readouterr().out
        header = out.splitlines()[0].split(" mutants for ", 1)[1].rstrip(":")
        locs = {line.strip().split(":", 1)[0] for line in out.splitlines()[1:] if line.strip()}
        assert header == rel  # the leading "./" is normalized away too
        assert locs == {rel}  # header and every per-mutant line agree, both POSIX


def test_list_mutants_marks_ignored_and_warns_on_unknown_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run still LISTS a suppressed mutant (it's generated), flagged `(ignored: reason)`; and
    # `ignore[<typo>]` naming no real operator warns on stderr (a silent no-op made visible).
    path = tmp_path / "f.gd"
    path.write_text(
        "func f(a):\n"
        "\tif a > 0:  # gdmutant: ignore[comparison] equiv\n"
        "\t\ta = a  # gdmutant: ignore[bogus]\n"
        "\treturn a > 1  # gdmutant: ignore[]\n",
        encoding="utf-8",
    )
    rc = list_mutants(str(path))
    assert rc == 0
    cap = capsys.readouterr()
    assert "comparison  > -> >=  (ignored: equiv)" in cap.out  # suppressed mutant listed + reason
    assert "bogus" in cap.err  # unknown operator name warned (a no-op)
    assert "empty brackets" in cap.err  # `ignore[]` warned too


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
            "--runner",
            "command",
            "--command",
            "c",
            "--godot",
            "g",
            "--tests",
            "t",
            "--timeout",
            "1",
            "--require-clean",
            "--json",
            "j",
        ]
    )
    # Every run-only flag, in declaration order — pins each label (--runner/--command included) and
    # the ", " join, so a mutated label or a dropped flag is caught.
    assert capsys.readouterr().err.strip() == (
        "note: --dry-run runs no tests, so --project, --runner, --command, --godot, --tests, "
        "--timeout, --require-clean, --json are ignored"
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
    assert args.source == ["f.gd"]  # nargs="+" -> a list, even for one path
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
    assert not hasattr(args, "report_path")  # no --report-path flag; the runner picks it itself
    assert args.timeout is None  # default: derived per-mutant from the baseline run time
    assert args.require_clean is False
    assert args.runner is None  # no silent default — main() refuses a real run without one
    assert args.test_command is None


def test_parser_rejects_an_unknown_runner() -> None:
    # `choices` must reject a runner it doesn't know — a mutant that drops the choices list would
    # silently accept "nope".
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "x.gd", "--runner", "nope"])


def test_parser_accepts_all_runner_choices() -> None:
    # Every valid runner parses — pins each choice literal (so "gdunit4" -> "GDUNIT4" is caught: an
    # explicit --runner gdunit4 would then be rejected). gut is a peer JUnit adapter (ADR-0011).
    for name in ("gdunit4", "gut", "command"):
        assert build_parser().parse_args(["run", "x.gd", "--runner", name]).runner == name


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
    assert "mutate GDScript files and report survivors\n" in top  # the run subcommand's help
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--help"])
    run_help = capsys.readouterr().out
    for expected in (
        "one or more .gd files or directories to mutate (a directory mutates every .gd under it, recursively, excluding addons/ and dot-dirs)",  # noqa: E501
        "the Godot project dir (default: the source's dir)",
        "test runner: gdunit4 or gut (both JUnit XML) or command (any harness, by exit code). "
        "Required, no default. Set it here or once in .gdmutant.toml",
        "test command for --runner command (exit 0 = pass), e.g. 'godot --headless --script res://tests/run_tests.gd'",  # noqa: E501
        "the Godot executable (default: godot)",
        "the test directory (gdunit4's -a / gut's -gdir) (default: res://test)",
        "per-mutant test-run timeout, in seconds (default: derived from the baseline run: 10x its wall-clock, so a hanging mutant is caught in seconds, not minutes)",  # noqa: E501
        "refuse to run if the source file has uncommitted git changes (default: warn only)",
        "write the Stryker JSON report here (use - for stdout; bare --json defaults to a "
        "timestamped filename)",
        "list the mutants without running any tests (no Godot needed)",
    ):
        assert f"{expected}\n" in run_help
    assert "--report-path" not in run_help  # the flag is gone, not just undocumented


def test_main_dispatches_run_with_injected_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _gd(tmp_path)
    # Replace the real GdUnit4Runner so no Godot is needed.
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    rc = main(["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4"])
    assert rc == 0
    assert "Mutation score:" in capsys.readouterr().out


@dataclass
class RecordingRunner:
    """Records the project_dir it was called with (all-pass, so mutants survive)."""

    seen: list[str] = field(default_factory=list)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.seen.append(project_dir)
        return SuiteResult(tests=3, failures=0, errors=0)


def test_main_default_project_uses_the_source_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gd(tmp_path)
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: runner)
    assert main(["run", str(path), "--runner", "gdunit4"]) == 0  # no --project given
    assert runner.seen[0] == str(path.resolve().parent)  # defaulted to the source's directory


# --- .gdmutant.toml config file ---------------------------------------------------------


def test_load_config_absent_returns_empty(tmp_path: Path) -> None:
    assert _load_config(tmp_path / ".gdmutant.toml") == {}


def test_load_config_maps_flag_named_keys_to_dests(tmp_path: Path) -> None:
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text(
        'project = "p"\ncommand = "c"\ntimeout = 30\nrequire-clean = true\n',
        encoding="utf-8",
    )
    assert _load_config(cfg) == {
        "project": "p",
        "test_command": "c",  # `command` maps to the test_command dest
        "timeout": 30,
        "require_clean": True,
    }


def test_load_config_warns_on_removed_report_path_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `report-path` is no longer a recognized config key — it warns and is skipped, same as any
    # other unknown key, rather than being silently accepted and then ignored.
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('report-path = "r"\n', encoding="utf-8")
    assert _load_config(cfg) == {}
    assert "unknown key 'report-path'" in capsys.readouterr().err


def test_load_config_warns_and_skips_unknown_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('godot = "g"\nnope = 1\n', encoding="utf-8")
    assert _load_config(cfg) == {"godot": "g"}
    assert "unknown key 'nope'" in capsys.readouterr().err


def test_load_config_rejects_malformed_toml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text("x = = broken", encoding="utf-8")
    assert _load_config(cfg) is None
    assert "cannot read" in capsys.readouterr().err


def test_load_config_rejects_bad_typed_values(tmp_path: Path) -> None:
    # set_defaults bypasses argparse's type/choices checks, so these must be caught at load.
    for name, body in (
        ("t1", 'timeout = "slow"'),  # not a number
        ("t2", "timeout = true"),  # a bool is not a valid number here
        ("t3", 'runner = "nope"'),  # not a valid runner
        ("t4", 'require-clean = "yes"'),  # not a bool
        ("t5", "project = 123"),  # string setting as int
        ("t6", "command = true"),  # string setting as bool
        ("t7", "godot = 456"),  # string setting as int
        ("t8", "tests = false"),  # string setting as bool
    ):
        cfg = tmp_path / f"{name}.toml"
        cfg.write_text(body, encoding="utf-8")
        assert _load_config(cfg) is None, body


def test_load_config_accepts_every_runner_choice(tmp_path: Path) -> None:
    # All three runners (gdunit4, gut, command) are valid config values — pins that "gut" is
    # accepted (a peer JUnit adapter, ADR-0011), not rejected like "nope" above.
    for runner in ("gdunit4", "gut", "command"):
        cfg = tmp_path / f"{runner}.toml"
        cfg.write_text(f'runner = "{runner}"\n', encoding="utf-8")
        assert _load_config(cfg) == {"runner": runner}


def test_main_uses_config_defaults_when_cli_flags_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `.gdmutant.toml` supplies --project; with no CLI --project, the runner gets it. Point
    # _CONFIG_FILENAME at the tmp file rather than chdir()-ing into it — a global cwd change breaks
    # mutmut's mutated-module resolution (`import gdmutant` → <cwd>/gdmutant), failing the dogfood.
    path = _gd(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = tmp_path / ".gdmutant.toml"
    # TOML literal strings (single-quoted) take their content byte-for-byte, no escape
    # processing -- the correct way to write an arbitrary path (esp. a Windows one, with
    # backslashes) into TOML. Python's repr() escapes backslashes for *Python's own* syntax,
    # which isn't TOML's, so tomllib was reading back a doubled-backslash value on Windows.
    cfg.write_text(f"project = '{proj}'\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: runner)
    # A config-set `project` is trust-required: it becomes the cwd of every subprocess and, under
    # --jobs, a tree shutil.copytree'd once per worker.
    assert main(["run", str(path), "--runner", "gdunit4", "--trust-config"]) == 0
    assert runner.seen[0] == str(proj)  # from config, not the source's own directory


def test_main_cli_flag_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _gd(tmp_path)
    cfg_proj, cli_proj = tmp_path / "cfg", tmp_path / "cli"
    cfg_proj.mkdir()
    cli_proj.mkdir()
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text(f"project = '{cfg_proj}'\n", encoding="utf-8")  # TOML literal string; see above
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: runner)
    # `project` is trust-required even though the CLI flag wins in the end: the config file still
    # set it, and that alone needs --trust-config (see _untrusted_config_keys).
    assert (
        main(
            [
                "run",
                str(path),
                "--project",
                str(cli_proj),
                "--runner",
                "gdunit4",
                "--trust-config",
            ]
        )
        == 0
    )
    assert runner.seen[0] == str(cli_proj)  # an explicit CLI flag wins over the config default


def test_main_malformed_config_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text("x = = broken", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    assert main(["run", "f.gd"]) == 2  # setup error, before anything runs
    assert "cannot read" in capsys.readouterr().err


def test_config_require_clean_can_be_overridden_off_by_cli(tmp_path: Path) -> None:
    # Follow-up: a config `require-clean = true` must be overridable back OFF for a single
    # run via --no-require-clean — the old store_true action had no such escape hatch.
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text("require-clean = true\n", encoding="utf-8")
    parser = build_parser(_load_config(cfg))
    assert parser.parse_args(["run", "f.gd"]).require_clean is True  # config default applies
    assert parser.parse_args(["run", "f.gd", "--no-require-clean"]).require_clean is False  # off
    assert parser.parse_args(["run", "f.gd", "--require-clean"]).require_clean is True  # on


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
            "--runner",
            "gdunit4",
            "--godot",
            "godot4",
            "--tests",
            "res://custom",
        ]
    )
    # timeout is passed too — defaulted here (not on the command line). No report_path kwarg: with
    # no --report-path flag, main() no longer threads one through — GdUnit4Runner's own dataclass
    # default (DEFAULT_REPORT_PATH) applies.
    assert captured == {
        "test_path": "res://custom",
        "godot": "godot4",
        "timeout": 600.0,
    }


def test_main_threads_timeout_to_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # --timeout must reach the GdUnit4Runner (parsed value, coerced to float). A mutant that drops
    # the kwarg or skips the type= coercion fails.
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
            "--runner",
            "gdunit4",
            "--timeout",
            "42",
        ]
    )
    assert "report_path" not in captured  # no longer threaded by main() at all
    assert captured["timeout"] == 42.0 and isinstance(captured["timeout"], float)


def test_main_threads_json_path_to_run_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --json must reach run_mutation as json_path; a mutant that nulls/drops it writes no report.
    path = _gd(tmp_path)
    report = tmp_path / "out.json"
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    rc = main(
        ["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4", "--json", str(report)]
    )
    assert rc == 0
    assert report.exists()


# --- bare --json/--html: a default, timestamped filename instead of a required path -------------


def test_default_report_stem_is_deterministic_and_filesystem_safe() -> None:
    stem = _default_report_stem(["corpus/turn_order.gd"], datetime(2026, 8, 2, 23, 55, 54))
    assert stem == "gdmutant-report-turn_order-20260802-235554"
    assert ":" not in stem, "Windows forbids a colon in a filename"


def test_default_report_stem_names_a_multi_file_run_by_count() -> None:
    stem = _default_report_stem(
        ["corpus/turn_order.gd", "corpus/other.gd"], datetime(2026, 8, 2, 23, 55, 54)
    )
    assert stem == "gdmutant-report-turn_order+1more-20260802-235554"


def test_resolve_default_report_paths_leaves_none_and_explicit_values_alone() -> None:
    assert _resolve_default_report_paths(None, None, ["f.gd"]) == (None, None)
    assert _resolve_default_report_paths("out.json", "out.html", ["f.gd"]) == (
        "out.json",
        "out.html",
    )
    # "-" (stdout) is an explicit value a caller typed, never the bare-flag sentinel.
    assert _resolve_default_report_paths("-", None, ["f.gd"]) == ("-", None)


def test_resolve_default_report_paths_only_resolves_the_sentinel() -> None:
    json_path, html_path = _resolve_default_report_paths(_DEFAULT_REPORT, "explicit.html", ["f.gd"])
    assert (
        json_path is not None
        and json_path.startswith("gdmutant-report-")
        and json_path.endswith(".json")
    )
    assert html_path == "explicit.html"

    # ...and the reverse: only --html was bare, so only html_path resolves.
    json_path, html_path = _resolve_default_report_paths(None, _DEFAULT_REPORT, ["f.gd"])
    assert json_path is None
    assert html_path is not None and html_path.startswith("gdmutant-report-")
    assert html_path.endswith(".html")


def test_resolve_default_report_paths_shares_one_stem_when_both_are_bare() -> None:
    # So a --json --html run's two files visibly pair up rather than landing under two different
    # timestamps seconds apart.
    json_path, html_path = _resolve_default_report_paths(_DEFAULT_REPORT, _DEFAULT_REPORT, ["f.gd"])
    assert json_path is not None and html_path is not None
    assert json_path[: -len(".json")] == html_path[: -len(".html")]


def test_resolve_default_report_paths_names_what_was_run() -> None:
    json_path, _ = _resolve_default_report_paths(_DEFAULT_REPORT, None, ["corpus/turn_order.gd"])
    assert json_path is not None and "turn_order" in json_path


def test_main_bare_json_and_html_resolve_to_paired_timestamped_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolution must happen inside main(), before run_mutation is ever called -- captured here by
    # replacing run_mutation itself, rather than writing real files, so this needs no chdir (a real
    # write of a *bare* --json/--html would otherwise land relative to the real process cwd, which
    # this suite must never change -- see test_mutation_baseline_inputs.py's hard rule on that).
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    rc = main(
        ["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4", "--json", "--html"]
    )
    assert rc == 0
    json_path, html_path = captured["json_path"], captured["html_path"]
    assert isinstance(json_path, str) and isinstance(html_path, str)
    assert json_path.startswith("gdmutant-report-") and json_path.endswith(".json")
    assert json_path[: -len(".json")] == html_path[: -len(".html")]


def test_main_dry_run_reports_a_bare_json_flag_as_ignored_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sentinel is resolved before the --dry-run "ignored flags" note reads args.json_path, so
    # this must show a real (if never-written) filename, never a raw sentinel object repr.
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--dry-run", "--json"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "--json" in err and "object at 0x" not in err
    assert not list(tmp_path.glob("gdmutant-report-*"))


def test_main_command_runner_builds_from_shlex_split_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --runner command builds a CommandRunner whose command is the shlex-split --command string,
    # with --timeout threaded through — and it does NOT build a GdUnit4Runner.
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_command_runner(**kwargs: object) -> RecordingRunner:
        captured.update(kwargs)
        return RecordingRunner()

    def boom_gdunit(**kwargs: object) -> object:
        raise AssertionError("GdUnit4Runner must not be built for --runner command")

    monkeypatch.setattr(cli, "CommandRunner", fake_command_runner)
    monkeypatch.setattr(cli, "GdUnit4Runner", boom_gdunit)
    rc = main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--runner",
            "command",
            "--command",
            "godot --headless --script res://tests/run.gd",
            "--timeout",
            "30",
        ]
    )
    assert rc == 0
    assert captured["command"] == ["godot", "--headless", "--script", "res://tests/run.gd"]
    assert captured["timeout"] == 30.0


def test_main_gut_runner_builds_from_tests_and_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --runner gut builds a GutRunner from --tests (-> test_dir) and --godot, threading no
    # report_path (no --report-path flag; GUT's own dataclass default, reports/gut_results.xml,
    # applies), and must NOT build a GdUnit4Runner. Pins the peer-adapter
    # wiring (ADR-0011).
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_gut_runner(**kwargs: object) -> RecordingRunner:
        captured.update(kwargs)
        return RecordingRunner()

    def boom_gdunit(**kwargs: object) -> object:
        raise AssertionError("GdUnit4Runner must not be built for --runner gut")

    monkeypatch.setattr(cli, "GutRunner", fake_gut_runner)
    monkeypatch.setattr(cli, "GdUnit4Runner", boom_gdunit)
    rc = main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--runner",
            "gut",
            "--godot",
            "godot4",
            "--tests",
            "res://gut_test",
        ]
    )
    assert rc == 0
    assert captured == {
        "test_dir": "res://gut_test",
        "godot": "godot4",
        "timeout": 600.0,
    }


def test_main_command_runner_requires_the_command_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --runner command with no --command is a usage error (exit 2), reported before anything runs.
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--project", str(tmp_path), "--runner", "command"])
    assert rc == 2
    assert (
        capsys.readouterr().err.strip() == "error: --runner command requires a non-empty --command"
    )


def test_main_command_runner_rejects_a_whitespace_only_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A whitespace-only --command shlex-splits to [], which would otherwise crash deep in the run
    # with a confusing IndexError; it must hit the same clean usage error (exit 2).
    path = _gd(tmp_path)
    rc = main(
        ["run", str(path), "--project", str(tmp_path), "--runner", "command", "--command", "   "]
    )
    assert rc == 2
    assert (
        capsys.readouterr().err.strip() == "error: --runner command requires a non-empty --command"
    )


def test_main_command_runner_rejects_unbalanced_quotes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An unbalanced quote makes shlex.split raise ValueError; surface it as a clean exit 2 with a
    # message, not a raw traceback out of main().
    path = _gd(tmp_path)
    rc = main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--runner",
            "command",
            "--command",
            'godot "unterminated',
        ]
    )
    assert rc == 2
    assert capsys.readouterr().err.startswith("error: could not parse --command:")


def test_split_command_posix_uses_default_shlex(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a POSIX host, the standard (correct) shlex default applies: quotes strip, forward-slash
    # paths pass through untouched, and a quoted path with spaces stays one token.
    monkeypatch.setattr(cli.os, "name", "posix")
    assert cli._split_command("godot --script res://t/run.gd") == [
        "godot",
        "--script",
        "res://t/run.gd",
    ]
    assert cli._split_command('godot --script "res://my test/run.gd"') == [
        "godot",
        "--script",
        "res://my test/run.gd",
    ]


def test_split_command_windows_keeps_backslash_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # The path parsing bug: default POSIX shlex eats backslashes, turning C:\Godot\godot.exe into
    # C:Godotgodot.exe ("runner not found"). On Windows we must keep them literal. This assertion
    # fails on the old posix-only split.
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._split_command(r"C:\Godot\godot.exe --headless res://t/run.gd") == [
        r"C:\Godot\godot.exe",
        "--headless",
        "res://t/run.gd",
    ]


def test_split_command_windows_forward_slash_path_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A forward-slash Windows path (which CreateProcess accepts) is what many users naturally type;
    # it must pass through with no mangling and no rewrite.
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._split_command("C:/Godot/godot.exe --headless") == [
        "C:/Godot/godot.exe",
        "--headless",
    ]


def test_split_command_windows_strips_quotes_from_spaced_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-POSIX lexing leaves a quoted token's surrounding quotes in place; a spaced path like
    # "C:\Program Files\Godot\godot.exe" must have them stripped so CreateProcess can resolve it.
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._split_command(r'"C:\Program Files\Godot\godot.exe" --headless') == [
        r"C:\Program Files\Godot\godot.exe",
        "--headless",
    ]


def test_split_command_windows_rewrites_msys_drive_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # An MSYS/Git-Bash drive path (/c/Godot/godot.exe) is not resolvable by CreateProcess; it must
    # be rewritten to Windows form. No lexer mode fixes this — it's a path-shape rewrite.
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._split_command("/c/Godot/godot.exe --headless") == [
        r"C:\Godot\godot.exe",
        "--headless",
    ]


def test_split_command_windows_unbalanced_quote_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Switching to posix=False on Windows must not lose the unbalanced-quote guard the CLI relies on
    # to return a clean exit-2 (rather than a raw traceback).
    monkeypatch.setattr(cli.os, "name", "nt")
    with pytest.raises(ValueError, match="No closing quotation"):
        cli._split_command('godot "unterminated')


def test_main_command_without_runner_command_is_flagged_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --command on a JUnit runner (not --runner command) is a footgun: warn instead of silently
    # discarding it (and still build the GdUnit4 runner).
    path = _gd(tmp_path)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    rc = main(
        [
            "run",
            str(path),
            "--project",
            str(tmp_path),
            "--runner",
            "gdunit4",
            "--command",
            "godot --headless",
        ]
    )
    assert rc == 0
    # Trailing newline so a wrapped "XX...XX" mutant (which keeps the text a substring) still fails.
    assert "note: --command is ignored unless --runner command is set\n" in capsys.readouterr().err


def test_main_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out  # the help text, not just a silent exit 0


# ---- warn before mutating an uncommitted working tree ----------------------------------


# Exact messages, pinned so mutmut's string mutation is caught.
def _dirty_warning(source: str) -> str:
    return (
        f"warning: {source} has uncommitted changes: gdmutant mutates it in place "
        "(restoring it when done), so a hard kill could leave it modified. Commit or stash "
        "first to be safe."
    )


def _require_clean_error(source: str) -> str:
    return (
        f"error: --require-clean was given, but {source} has uncommitted changes.\n"
        "  gdmutant edits the file where it lies, so it will not start without a copy it "
        "could put back.\n"
        "  Commit or stash first to be safe. Or re-run with --no-require-clean to accept "
        "the risk."
    )


def test_git_backup_reports_no_copy_when_modified(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path)
    (repo / "f.gd").write_text(
        "func f(a, b) -> bool:\n\treturn a >= b\n", encoding="utf-8"
    )  # now modified
    assert _git_backup(str(repo / "f.gd")).backed_up is False


def test_git_backup_reports_a_copy_when_committed_and_clean(tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path)  # committed, unmodified
    assert _git_backup(str(repo / "f.gd")).backed_up is True


def test_git_backup_reports_no_copy_when_untracked(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    path = _gd(tmp_path)  # written but never `git add`ed
    assert _git_backup(str(path)).backed_up is False


def test_git_backup_handles_a_dash_prefixed_filename(tmp_path: Path) -> None:
    # The `--` before the pathspec matters for a filename starting with "-" (git would otherwise
    # parse it as an option). A dash-named, untracked file must still report dirty — this pins the
    # `--` so dropping/mangling it is caught.
    _git(tmp_path, "init")
    weird = tmp_path / "-weird.gd"
    weird.write_text("func f() -> int:\n\treturn 1\n", encoding="utf-8")
    assert _git_backup(str(weird)).backed_up is False


def test_git_backup_is_unknown_outside_a_repo(tmp_path: Path) -> None:
    # No repo at all. This must read as "cannot tell", NOT as "git has a copy of it" -- those were
    # the same answer before, which is how --require-clean came to pass without checking anything.
    path = _gd(tmp_path)
    backup = _git_backup(str(path))
    assert backup.backed_up is None
    assert "not a git repository" in backup.reason


def test_git_backup_is_unknown_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git not installed (subprocess raises FileNotFoundError) must be swallowed, not crash -- but
    # it must also not be mistaken for a clean tree, which is what --require-clean then trusted.
    path = _gd(tmp_path)

    def raise_fnf(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cli.subprocess, "run", raise_fnf)
    backup = _git_backup(str(path))
    assert backup.backed_up is None
    assert "may not be installed" in backup.reason


def test_git_helper_isolated_from_hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest run from a git hook inherits GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE pointing at
    # the hook's repo. `_git` must scrub them so `git init` lands in the intended dir, not the decoy
    # GIT_DIR. Without the scrub, git would create the repo at GIT_DIR and leave `work/.git` absent.
    decoy = tmp_path / "decoy.git"
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "decoy_worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "decoy.index"))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init")
    assert (work / ".git").exists()  # init acted on `work` (env scrubbed)
    assert not decoy.exists()  # ...not on the leaked GIT_DIR


def test_git_backup_ignores_leaked_hook_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: production `_git_backup` (the ``git status
    --porcelain`` call) must scrub GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE itself — it must not lean on
    conftest's autouse `_isolate_git_env` fixture, which would re-mask the very leak this test is
    meant to pin. `monkeypatch.setenv` below runs inside the test body, i.e. *after* that fixture's
    per-test cleanup already ran, so it faithfully reproduces gdmutant being invoked from inside a
    git hook regardless of the autouse scrub.

    Without the production scrub, `git status --porcelain -- f.gd` (cwd=target, but GIT_DIR/
    GIT_WORK_TREE repointed at `decoy`) matches no path in the decoy repo and prints nothing — a
    dirty target file would misread as clean, silently defeating --require-clean.
    """
    decoy = tmp_path / "decoy"
    _init_decoy_repo(decoy)

    target = tmp_path / "target"
    target.mkdir()
    _committed_repo(target)
    path = target / "f.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a >= b and a < b\n", encoding="utf-8")  # dirty

    for key, value in _leak_decoy_env(decoy).items():
        monkeypatch.setenv(key, value)

    assert _git_backup(str(path)).backed_up is False


@dataclass
class RunBoomRunner:
    """A runner that's cheap to construct but explodes if actually run — proves --require-clean
    short-circuits before any suite (Godot) invocation."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise AssertionError("--require-clean must return before running the suite")


def test_run_mutation_warns_on_dirty_tree_but_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A dirty source warns (in-place mutation is risky under a hard kill) but does NOT block —
    # gdmutant is driven by headless agents, so it must never wait on an interactive prompt.
    repo = _committed_repo(tmp_path)
    path = repo / "f.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a >= b and a < b\n", encoding="utf-8")  # dirty
    rc = run_mutation(str(path), str(repo), MarkerRunner(str(path), "ZZZ"))
    assert rc == 0  # warned, then ran
    assert _dirty_warning(str(path)) in capsys.readouterr().err


def test_run_mutation_no_warning_on_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _committed_repo(tmp_path)  # committed, unmodified => clean
    path = repo / "f.gd"
    rc = run_mutation(str(path), str(repo), MarkerRunner(str(path), "ZZZ"))
    assert rc == 0
    assert "uncommitted changes" not in capsys.readouterr().err  # silent when clean


def test_run_mutation_require_clean_refuses_dirty_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # require_clean turns the warning into a hard exit 2, without ever running the suite (the
    # RunBoomRunner would raise if reached) — so it's enforceable with no Godot present.
    repo = _committed_repo(tmp_path)
    path = repo / "f.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a >= b\n", encoding="utf-8")  # dirty
    rc = run_mutation(str(path), str(repo), RunBoomRunner(), require_clean=True)
    assert rc == 2
    assert capsys.readouterr().err == _require_clean_error(str(path)) + "\n"


def test_run_mutation_require_clean_allows_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # require_clean is a no-op on a clean tree: the run proceeds normally.
    repo = _committed_repo(tmp_path)
    path = repo / "f.gd"
    rc = run_mutation(str(path), str(repo), MarkerRunner(str(path), "ZZZ"), require_clean=True)
    assert rc == 0
    assert "uncommitted changes" not in capsys.readouterr().err


def test_run_mutation_read_error_precedes_dirty_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ordering (the re-review nit): a tracked-but-deleted source is BOTH dirty (git sees the
    # deletion) and unreadable. The read error must come first, with no dirty-tree warning about a
    # run that was never going to start printed ahead of it. Guards the git check sitting after
    # _load_gdscript.
    repo = _committed_repo(tmp_path)
    (repo / "f.gd").unlink()  # deleted => dirty AND unreadable
    rc = run_mutation(str(repo / "f.gd"), str(repo), MarkerRunner("x", "y"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "uncommitted changes" not in err  # no dirty-warning before the read error


def test_main_threads_require_clean_to_run_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --require-clean must reach run_mutation as require_clean=True, and default to False otherwise.
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    main(["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4", "--require-clean"])
    assert captured["require_clean"] is True
    captured.clear()
    main(["run", str(path), "--project", str(tmp_path), "--runner", "gdunit4"])
    assert captured["require_clean"] is False
    captured.clear()
    # If the config defaults it to True, --no-require-clean must override it to False. Point
    # _CONFIG_FILENAME at the file rather than chdir()-ing — a cwd change breaks mutmut's stats
    # collection (it resolves source_paths=["gdmutant"] relative to cwd).
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text("require-clean = true\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    main(["run", str(path), "--runner", "gdunit4", "--no-require-clean"])
    assert captured["require_clean"] is False


# --- multi-file / directory targets ------------------------------------------------------


def _multi_project(tmp_path: Path) -> tuple[str, str]:
    """A tmp project: two .gd sources (one nested) + an addons/ file and a .godot/ file that a
    directory target must SKIP. Returns the two real source paths (sorted)."""
    (tmp_path / "a.gd").write_text("func f(x) -> bool:\n\treturn x > 0\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.gd").write_text("func g(x) -> bool:\n\treturn x < 0\n", encoding="utf-8")
    addon = tmp_path / "addons" / "gdUnit4"
    addon.mkdir(parents=True)
    (addon / "ignored.gd").write_text("func h():\n\tpass\n", encoding="utf-8")
    dot = tmp_path / ".godot"
    dot.mkdir()
    (dot / "cache.gd").write_text("func c():\n\tpass\n", encoding="utf-8")
    return str(tmp_path / "a.gd"), str(sub / "b.gd")


def test_expand_sources_directory_recurses_skipping_addons_and_dotdirs(tmp_path: Path) -> None:
    a, b = _multi_project(tmp_path)
    assert cli._expand_sources([str(tmp_path)]) == sorted([a, b])  # addons/ + .godot/ skipped


def _write_test_suites(tmp_path: Path) -> list[str]:
    """Drop every GdUnit4/GUT test-file shape into `tmp_path` — a `test/` dir, the three name
    affixes (``*_test.gd`` / ``test_*.gd`` / ``*Test.gd``), and two unconventionally-named files the
    only-robust content signal must catch: a bare ``extends GdUnitTestSuite`` and the single-line
    ``class_name Foo extends GutTest`` form. Returns the paths a directory target must skip."""
    suite = "extends GdUnitTestSuite\nfunc test_it() -> void:\n\tpass\n"
    (tmp_path / "player_test.gd").write_text(suite, encoding="utf-8")  # GdUnit4 snake_case
    (tmp_path / "test_player.gd").write_text(suite, encoding="utf-8")  # GUT / getting-started
    (tmp_path / "PlayerTest.gd").write_text(suite, encoding="utf-8")  # GdUnit4 PascalCase
    tdir = tmp_path / "test"
    tdir.mkdir()
    (tdir / "in_test_dir.gd").write_text(suite, encoding="utf-8")  # under a test/ directory
    # Oddly named (no affix, not under test/) but the base class gives it away — the robust signal.
    (tmp_path / "checks.gd").write_text(
        "extends GutTest\nfunc test_x() -> void:\n\tpass\n", encoding="utf-8"
    )
    # ...including Godot's legal single-line `class_name Foo extends Base`, where `extends` is
    # mid-line (a bare `^extends` anchor would miss this — the exact noise this feature prevents).
    (tmp_path / "oddity.gd").write_text(
        "class_name Oddity extends GdUnitTestSuite\nfunc test_y() -> void:\n\tpass\n",
        encoding="utf-8",
    )
    return [
        str(tmp_path / "player_test.gd"),
        str(tmp_path / "test_player.gd"),
        str(tmp_path / "PlayerTest.gd"),
        str(tdir / "in_test_dir.gd"),
        str(tmp_path / "checks.gd"),
        str(tmp_path / "oddity.gd"),
    ]


def test_expand_sources_directory_skips_test_suites_and_notes_the_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    skipped = _write_test_suites(tmp_path)
    result = cli._expand_sources([str(tmp_path)])
    assert result == sorted([a, b])  # only the two real sources — every test suite dropped
    for path in skipped:
        assert path not in result
    err = capsys.readouterr().err
    assert "skipped 6 test file(s)" in err  # the count is surfaced, not silent
    assert "name one explicitly to mutate it" in err  # ...with the override escape hatch


def test_expand_sources_duplicate_dir_args_do_not_inflate_skip_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Passing the same directory twice must not double-count skipped test files — the note is meant
    # to be a trustworthy signal, and the file set is already de-duped (from review of #53).
    a, b = _multi_project(tmp_path)
    _write_test_suites(tmp_path)
    result = cli._expand_sources([str(tmp_path), str(tmp_path)])
    assert result == sorted([a, b])  # file set de-duped despite the repeated arg
    assert "skipped 6 test file(s)" in capsys.readouterr().err  # count de-duped too, not 12


def test_expand_sources_explicit_test_file_is_mutated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _multi_project(tmp_path)
    skipped = _write_test_suites(tmp_path)
    named = skipped[0]  # player_test.gd, named explicitly
    assert cli._expand_sources([named]) == [named]  # explicit path overrides the test skip
    assert "skipped" not in capsys.readouterr().err  # ...and prints no skip note


def test_expand_sources_exclude_glob_drops_matching_files_and_notes_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)  # a.gd + sub/b.gd
    # A filename glob drops b.gd anywhere; a path glob (`*/sub/*`) would too — assert the filename
    # form, the ergonomic case (no leading dirs needed).
    result = cli._expand_sources([str(tmp_path)], exclude=["b.gd"])
    assert result == [a]  # b.gd excluded, a.gd kept
    assert "excluded 1 file(s) matching --exclude" in capsys.readouterr().err


def test_expand_sources_exclude_does_not_touch_explicitly_named_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    # Same override rule as the test-skip: an exclude glob only narrows a *directory* expansion, so
    # naming the file directly still mutates it.
    assert cli._expand_sources([b], exclude=["b.gd"]) == [b]
    assert "excluded" not in capsys.readouterr().err


def test_expand_sources_exclude_note_silent_when_file_also_named_explicitly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The escape hatch in practice: exclude a glob for the dir scan, then name one file back in.
    # That file IS mutated, so the "excluded ... name one explicitly" note must not fire for it
    # (would tell the user to do what they just did). Flagged in review of #55.
    a, b = _multi_project(tmp_path)
    result = cli._expand_sources([str(tmp_path), b], exclude=["b.gd"])
    assert result == sorted([a, b])  # b.gd excluded from the scan but mutated via the explicit arg
    assert "excluded" not in capsys.readouterr().err


def test_expand_sources_skip_note_silent_when_test_file_also_named_explicitly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same rule for the test-skip note (the pre-existing analog review flagged): a test file both
    # under the dir scan and named explicitly is mutated, so it must not be counted as skipped.
    a, _b = _multi_project(tmp_path)
    suite = tmp_path / "player_test.gd"
    suite.write_text("extends GdUnitTestSuite\nfunc test_it():\n\tpass\n", encoding="utf-8")
    result = cli._expand_sources([str(tmp_path), str(suite)])
    assert str(suite) in result  # mutated via the explicit arg despite matching the test convention
    assert "skipped" not in capsys.readouterr().err


def test_expand_sources_exclude_matching_everything_errors_with_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _multi_project(tmp_path)
    assert cli._expand_sources([str(tmp_path)], exclude=["*.gd"]) is None  # nothing survives
    assert "every .gd file matched --exclude" in capsys.readouterr().err


def test_main_exclude_flag_combines_with_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # config `exclude` and a CLI --exclude are additive (both narrow the directory target).
    a, b = _multi_project(tmp_path)
    (tmp_path / "c.gd").write_text("func k(x) -> bool:\n\treturn x == 0\n", encoding="utf-8")
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('exclude = ["a.gd"]\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    # config drops a.gd; CLI drops c.gd → only b.gd is listed.
    assert main(["run", str(tmp_path), "--dry-run", "--exclude", "c.gd"]) == 0
    out = capsys.readouterr().out
    # --dry-run's console listing is POSIX-normalized regardless of host OS.
    assert (
        Path(b).as_posix() in out
        and Path(a).as_posix() not in out
        and (tmp_path / "c.gd").as_posix() not in out
    )


def test_load_config_rejects_non_list_exclude(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('exclude = "notalist"\n', encoding="utf-8")
    assert cli._load_config(cfg) is None
    assert "'exclude' must be a list of glob strings" in capsys.readouterr().err


def test_is_test_file_treats_undecodable_content_as_not_a_test(tmp_path: Path) -> None:
    # A .gd whose bytes aren't valid UTF-8 can't be content-checked; it isn't a test suite (the
    # content read swallows the error), so it stays in the mutation set rather than being dropped.
    blob = tmp_path / "weird.gd"  # not test-named, not under test/
    blob.write_bytes(b"extends Node\n\xff\xfe not utf-8\n")
    assert cli._is_test_file(blob, blob.relative_to(tmp_path)) is False


def test_expand_sources_dedupes_and_sorts_explicit_paths(tmp_path: Path) -> None:
    a, b = _multi_project(tmp_path)
    assert cli._expand_sources([b, a, a]) == sorted([a, b])  # duplicate a collapsed, sorted


def test_expand_sources_no_gd_files_returns_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli._expand_sources([str(empty)]) is None
    assert "no .gd files" in capsys.readouterr().err


def test_default_project_dir_dir_target_vs_file_target(tmp_path: Path) -> None:
    a, b = _multi_project(tmp_path)
    assert cli._default_project_dir([str(tmp_path)], [a, b]) == str(
        tmp_path
    )  # a dir IS the project
    assert cli._default_project_dir([a], [a]) == str(Path(a).resolve().parent)  # a file -> its dir


def test_run_mutation_paths_aggregates_and_writes_merged_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    report = tmp_path / "r.json"
    rc = cli.run_mutation_paths([a, b], str(tmp_path), RecordingRunner(), json_path=str(report))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 files:" in out and "Mutation score:" in out  # per-file breakdown + one aggregate
    data = json.loads(report.read_text(encoding="utf-8"))
    # Report keys are POSIX-normalized regardless of host OS.
    assert set(data["files"]) == {Path(a).as_posix(), Path(b).as_posix()}


def test_all_survived_warning_fires_on_a_multi_file_run_with_no_kills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Across every file the baseline passed but nothing was detected -> the aggregate warning fires.
    a, b = _multi_project(tmp_path)
    rc = cli.run_mutation_paths([a, b], str(tmp_path), RecordingRunner())
    assert rc == 0
    assert "evaluated mutants survived" in capsys.readouterr().err


def test_all_survived_warning_absent_on_a_multi_file_run_with_a_kill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One mutant killed anywhere means a test reaches the code -> not the vacuous case, no warning.
    a, b = _multi_project(tmp_path)
    rc = cli.run_mutation_paths([a, b], str(tmp_path), MarkerRunner(a, ">="))
    assert rc == 0
    assert "evaluated mutants survived" not in capsys.readouterr().err


def test_run_mutation_paths_baseline_failure_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    # marker present in a's ORIGINAL source -> the unmutated baseline "fails" for the whole pass.
    rc = cli.run_mutation_paths([a, b], str(tmp_path), MarkerRunner(a, ">"))
    assert rc == 1
    assert "unmutated test suite failed" in capsys.readouterr().err


def test_run_mutation_paths_bad_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, _b = _multi_project(tmp_path)
    rc = cli.run_mutation_paths([a, str(tmp_path / "nope.gd")], str(tmp_path), RecordingRunner())
    assert rc == 2  # a missing source fails before anything runs
    assert "cannot read" in capsys.readouterr().err


def test_run_mutation_paths_bad_project_dir_and_bad_report_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    assert cli.run_mutation_paths([a, b], str(tmp_path / "nope"), RecordingRunner()) == 2
    assert "project directory not found" in capsys.readouterr().err
    bad_json = tmp_path / "missing" / "r.json"
    assert (
        cli.run_mutation_paths([a, b], str(tmp_path), RecordingRunner(), json_path=str(bad_json))
        == 2
    )


def test_main_routes_a_directory_to_the_multi_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _multi_project(tmp_path)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    rc = main(["run", str(tmp_path), "--project", str(tmp_path), "--runner", "gdunit4"])
    assert rc == 0
    assert "2 files:" in capsys.readouterr().out  # routed to run_mutation_paths


def test_main_no_gd_files_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["run", str(empty)]) == 2
    assert "no .gd files" in capsys.readouterr().err


def test_main_dry_run_lists_each_file_under_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _multi_project(tmp_path)
    assert main(["run", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    # --dry-run's console listing is POSIX-normalized regardless of host OS.
    assert Path(a).as_posix() in out and Path(b).as_posix() in out  # every .gd under the dir


def test_run_mutation_paths_dirty_tree_warns_or_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A dirty source in a multi-file run warns by default, or refuses (exit 2) with --require-clean,
    # exactly like the single-file path.
    repo = _committed_repo(tmp_path)  # commits f.gd
    (repo / "g.gd").write_text("func g(x) -> bool:\n\treturn x < 0\n", encoding="utf-8")
    _git(repo, "add", "g.gd")
    _git(repo, "commit", "-m", "add g")
    (repo / "f.gd").write_text(
        "func f(a, b) -> bool:\n\treturn a >= b\n", encoding="utf-8"
    )  # dirty
    f, g = str(repo / "f.gd"), str(repo / "g.gd")
    assert cli.run_mutation_paths([f, g], str(repo), RecordingRunner(), require_clean=True) == 2
    assert "--require-clean was given, but" in capsys.readouterr().err
    assert (
        cli.run_mutation_paths([f, g], str(repo), RecordingRunner()) == 0
    )  # default: warn + proceed
    assert "uncommitted changes" in capsys.readouterr().err


# --- resilience: one unparseable file must not abort a multi-file run (GdUnit4) ---


def test_drop_unparseable_partitions_by_readability_and_parse(tmp_path: Path) -> None:
    good = tmp_path / "good.gd"
    good.write_text("func f():\n\treturn 1\n", encoding="utf-8")
    bad_parse = tmp_path / "bad.gd"
    bad_parse.write_text("func f(:\n", encoding="utf-8")  # doesn't parse
    bad_read = tmp_path / "binary.gd"
    bad_read.write_bytes(b"\xff\xfe not utf-8")  # can't be decoded
    # bad_read FIRST, then a good file — so a bug that stops at the first bad file (rather than
    # skipping it and continuing) would drop `good` and be caught.
    ok, dropped = cli._drop_unparseable([str(bad_read), str(good), str(bad_parse)])
    assert ok == [str(good)]
    assert set(dropped) == {str(bad_parse), str(bad_read)}


def test_main_dry_run_skips_an_unparseable_file_in_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One unparseable file is skipped with a warning and the rest are mutated — not an abort. This
    # was the old "abort at exit 2" behavior; the GdUnit4 dogfood surfaced it as an adoption bug.
    (tmp_path / "ok.gd").write_text("func f():\n\treturn 1\n", encoding="utf-8")
    (tmp_path / "bad.gd").write_text("func f(:\n", encoding="utf-8")  # not valid GDScript
    assert main(["run", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "ok.gd:" in out.out  # the good file is still listed
    assert "skipped 1 directory file(s) gdtoolkit couldn't parse" in out.err
    assert "bad.gd" in out.err


def test_main_real_run_skips_unparseable_and_mutates_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real (non-dry) path is resilient too: the filter runs before dispatch, so the mutation run
    # only ever sees the parseable files.
    (tmp_path / "a.gd").write_text("func f(x) -> bool:\n\treturn x > 0\n", encoding="utf-8")
    (tmp_path / "b.gd").write_text("func g(x) -> bool:\n\treturn x < 0\n", encoding="utf-8")
    (tmp_path / "bad.gd").write_text("func h(:\n", encoding="utf-8")
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    assert main(["run", str(tmp_path), "--project", str(tmp_path), "--runner", "gdunit4"]) == 0
    out = capsys.readouterr()
    assert "skipped 1 directory file(s)" in out.err
    assert "2 files:" in out.out  # the two parseable files were mutated as a multi-file run


def test_main_all_unparseable_directory_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.gd").write_text("func f(:\n", encoding="utf-8")
    (tmp_path / "b.gd").write_text("func g(:\n", encoding="utf-8")  # both invalid
    assert main(["run", str(tmp_path), "--dry-run"]) == 2
    # line-exact, so a mutation to the error text is caught (a substring check wouldn't be).
    assert (
        "error: no parseable .gd files in the given path(s)" in capsys.readouterr().err.splitlines()
    )


def test_main_single_explicit_unparseable_file_still_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Resilience is for directory-discovered files only — a lone explicitly-named bad file is a
    # direct request that failed, so it stays a hard exit-2.
    bad = tmp_path / "bad.gd"
    bad.write_text("func f(:\n", encoding="utf-8")
    assert main(["run", str(bad), "--dry-run"]) == 2
    assert "not valid GDScript" in capsys.readouterr().err


def test_main_two_explicit_files_one_unparseable_still_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The resilience gate keys on directory-discovered vs explicitly-named, NOT on file count:
    # naming two files where one can't parse must stay a hard exit-2, exactly as naming one does.
    # (A count-only gate silently dropped the 2nd explicit file and exited 0.)
    good = tmp_path / "good.gd"
    good.write_text("func f():\n\treturn 1\n", encoding="utf-8")
    bad = tmp_path / "bad_explicit.gd"
    bad.write_text("func f(:\n", encoding="utf-8")
    assert main(["run", str(good), str(bad), "--dry-run"]) == 2
    # No skip-note: an explicit file is never silently dropped.
    assert "skipped" not in capsys.readouterr().err


# --- diff-scoped / incremental mode (--since) --------------------------------------------

# Two mutable lines (2 and 4) so a change to one is distinguishable from the other under --since.
_TWO_LINE_SRC = "func f(x) -> bool:\n\treturn x > 0\nfunc g(x) -> bool:\n\treturn x < 0\n"


def _repo_with_committed(tmp_path: Path, name: str, text: str) -> str:
    """A git repo with `name` committed at HEAD; returns the file path."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    _git(tmp_path, "add", name)
    _git(tmp_path, "commit", "-m", "base")
    return str(path)


def test_changed_lines_maps_the_modified_line(tmp_path: Path) -> None:
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    # change line 2 only (the `> 0` return) — leave line 4 untouched
    Path(path).write_text(_TWO_LINE_SRC.replace("x > 0", "x > 1"), encoding="utf-8")
    changed = cli._changed_lines("HEAD", [path])
    assert changed == {str(Path(path).resolve()): {2}}  # the +side of the diff, line 2 only


def test_changed_lines_ignores_leaked_hook_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: production `_changed_lines` (``git diff --unified=0`` /
    ``git ls-files --error-unmatch``) must scrub GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE itself — it
    must not lean on conftest's autouse `_isolate_git_env` fixture, which would re-mask the very
    leak this test is meant to pin. `monkeypatch.setenv` below runs inside the test body, i.e.
    *after* that fixture's per-test cleanup already ran, so it faithfully reproduces gdmutant being
    invoked from inside a git hook regardless of the autouse scrub.

    Without the production scrub, ``git diff --unified=0 HEAD -- f.gd`` (cwd=target, but GIT_DIR/
    GIT_WORK_TREE repointed at `decoy`) diffs against the decoy repo, where the path doesn't exist —
    an empty diff — so --since would silently report "no lines changed" for a file that changed.
    """
    decoy = tmp_path / "decoy"
    _init_decoy_repo(decoy)

    target = tmp_path / "target"
    target.mkdir()
    path = _repo_with_committed(target, "f.gd", _TWO_LINE_SRC)
    # change line 2 only (the `> 0` return) — leave line 4 untouched
    Path(path).write_text(_TWO_LINE_SRC.replace("x > 0", "x > 1"), encoding="utf-8")

    for key, value in _leak_decoy_env(decoy).items():
        monkeypatch.setenv(key, value)

    changed = cli._changed_lines("HEAD", [path])
    assert changed == {str(Path(path).resolve()): {2}}


def test_all_git_subprocess_calls_scrub_leaked_hook_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit-level pin: every git subprocess.run call gdmutant makes
    (``git status``, ``git diff``, ``git ls-files``) must pass ``env=`` with all six inherited
    GIT_* location vars removed — checked directly against the live env kwarg, independent of any
    particular git version's real-repo error behavior. Spies on `cli.subprocess.run` (never
    touching a real git binary) so the three call sites in `_git_backup` and
    `_changed_lines` are each exercised and captured. Deliberately re-injects the leak vars via
    monkeypatch inside the test body (after conftest's autouse `_isolate_git_env` fixture already
    cleared them), so it does not depend on that fixture.
    """
    leaked = {
        "GIT_DIR": "decoy-dir",
        "GIT_WORK_TREE": "decoy-worktree",
        "GIT_INDEX_FILE": "decoy-index",
        "GIT_OBJECT_DIRECTORY": "decoy-objects",
        "GIT_COMMON_DIR": "decoy-common",
        "GIT_PREFIX": "decoy-prefix",
    }
    for key, value in leaked.items():
        monkeypatch.setenv(key, value)

    captured_envs: list[dict[str, str] | None] = []

    def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_envs.append(kwargs.get("env"))  # type: ignore[arg-type]
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", spy)

    path = _gd(tmp_path)  # a real file on disk; git itself is never actually invoked (spied above)
    cli._git_backup(str(path))  # -> git status
    cli._changed_lines("HEAD", [str(path)])  # -> git diff, then git ls-files (empty diff + is_file)

    assert len(captured_envs) == 3  # status, diff, ls-files — every call site hit exactly once
    for env in captured_envs:
        assert env is not None, "every git subprocess call must pass env="
        for key in leaked:
            assert key not in env, f"{key} leaked into a git subprocess call"


def test_changed_lines_bad_ref_returns_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert cli._changed_lines("no-such-ref", [path]) is None
    assert "git diff for --since no-such-ref failed" in capsys.readouterr().err


def test_changed_lines_treats_an_untracked_new_file_as_fully_changed(tmp_path: Path) -> None:
    # git diff is silent on a never-`git add`-ed file, so a brand-new .gd must be treated as fully
    # changed (every line new), not silently skipped as "no changes". Flagged in review of #61.
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "committed.gd").write_text("func a():\n\tpass\n", encoding="utf-8")
    _git(tmp_path, "add", "committed.gd")
    _git(tmp_path, "commit", "-m", "base")
    new = tmp_path / "new.gd"
    new.write_text(_TWO_LINE_SRC, encoding="utf-8")  # on disk but never staged
    assert cli._changed_lines("HEAD", [str(new)]) == {str(new.resolve()): {1, 2, 3, 4}}


def test_changed_lines_nonexistent_file_maps_to_empty(tmp_path: Path) -> None:
    # A path that doesn't exist on disk contributes no changed lines (its real error surfaces later
    # when the source can't be read) — the untracked "fully changed" path is guarded on `is_file`.
    _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    missing = str(tmp_path / "gone.gd")
    assert cli._changed_lines("HEAD", [missing]) == {str(Path(missing).resolve()): set()}


def test_changed_lines_git_unavailable_returns_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # If git can't even be launched (missing binary), --since is a setup error, not a silent no-op.
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli._changed_lines("HEAD", [str(tmp_path / "f.gd")]) is None
    assert "could not run git for --since HEAD" in capsys.readouterr().err


def test_diff_scoped_adapter_keeps_only_changed_line_mutants(tmp_path: Path) -> None:
    from gdmutant.adapters.gdscript import ADAPTER

    path = str(tmp_path / "f.gd")
    Path(path).write_text(_TWO_LINE_SRC, encoding="utf-8")
    scoped = cli._diff_scoped(ADAPTER, {str(Path(path).resolve()): {2}})
    from gdmutant.engine.operators import CATALOG

    mutants = scoped.generate_mutants(path, _TWO_LINE_SRC, CATALOG)
    assert mutants  # line 2 has `>` and `0` mutants
    assert {m.span.line for m in mutants} == {2}  # nothing from line 4's `< 0`


def test_main_since_dry_run_lists_only_changed_line_mutants(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    Path(path).write_text(_TWO_LINE_SRC.replace("x > 0", "x > 1"), encoding="utf-8")
    assert main(["run", path, "--dry-run", "--since", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "f.gd:2:" in out  # the changed line's mutants are listed
    assert "f.gd:4:" not in out  # the untouched line's are not


def test_main_since_no_changes_is_a_clean_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing changed since HEAD → nothing to mutate: exit 0 with a note, no Godot invoked.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD"]) == 0
    assert "no lines changed since HEAD" in capsys.readouterr().err


def test_main_since_no_changes_writes_an_empty_report_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The defect this pins: `--since` with nothing changed exited 0 having written no report at all,
    # and its one explanation went to stderr. A CI script capturing stdout for json.loads got an
    # empty string, so "nothing to mutate" and "the tool broke" arrived looking identical. It now
    # answers on the channel the caller is reading.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--json", "-"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["schemaVersion"] == "2"
    # Report keys are POSIX-normalized regardless of host OS.
    entry = data["files"][Path(path).as_posix()]  # the file is present, no special case needed
    assert entry["mutants"] == []
    assert entry["language"] == "gdscript"
    assert entry["source"] == Path(path).read_text(encoding="utf-8")
    assert "no lines changed since HEAD" in captured.err  # the human note is unchanged


def test_main_since_no_changes_lists_every_given_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every file the run was pointed at appears, not just the first — a report that dropped files
    # would read as "these were mutated and nothing survived", which is a different claim.
    first = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    second = tmp_path / "g.gd"
    second.write_text(_TWO_LINE_SRC, encoding="utf-8")
    _git(tmp_path, "add", "g.gd")
    _git(tmp_path, "commit", "-m", "add g.gd")
    assert main(["run", first, str(second), "--since", "HEAD", "--json", "-"]) == 0
    data = json.loads(capsys.readouterr().out)
    # Report keys are POSIX-normalized regardless of host OS.
    assert set(data["files"]) == {Path(first).as_posix(), second.as_posix()}
    assert all(entry["mutants"] == [] for entry in data["files"].values())


def test_main_since_no_changes_writes_an_empty_report_to_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--json <path>` and `--html <path>` are honoured on this path too: a CI step that always
    # uploads its report artifact must not find the file missing on the no-change runs.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    report = tmp_path / "r.json"
    page = tmp_path / "r.html"
    assert main(["run", path, "--since", "HEAD", "--json", str(report), "--html", str(page)]) == 0
    # Report keys are POSIX-normalized regardless of host OS.
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["files"][Path(path).as_posix()]["mutants"] == []
    assert "<html" in page.read_text(encoding="utf-8")
    assert "Wrote report to" in capsys.readouterr().out


def test_main_since_no_changes_relativizes_the_html_report_to_the_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The empty report is a real report, so it gets the same `--project` treatment: paths on the
    # page are relative to the project root, not absolute ones carrying the author's directory
    # layout. This is also what pins that the no-change path resolves the project dir at all.
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    source = tmp_path / "src" / "f.gd"
    source.parent.mkdir()
    source.write_text(_TWO_LINE_SRC, encoding="utf-8")
    _git(tmp_path, "add", "src/f.gd")
    _git(tmp_path, "commit", "-m", "base")
    page = tmp_path / "r.html"
    assert (
        main(
            [
                "run",
                str(source),
                "--project",
                str(tmp_path),
                "--since",
                "HEAD",
                "--html",
                str(page),
            ]
        )
        == 0
    )
    assert '"path": "src/f.gd"' in page.read_text(encoding="utf-8")


# --- the no-change path enforces the same contract as a real run ------------------------------
#
# `_no_changes_report` is gdmutant's third report-producing path, and it was written with a subset
# of the preflights the two real ones enforce. A caller cannot tell from the outside which path
# served their run, so a guarantee that holds on two paths out of three is not a guarantee. These
# pin each check on the path that was missing it; `_setup_problem` is the shared preflight that
# stops the three from drifting again.


def test_main_since_no_changes_refuses_the_step_summary_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Was: exit 0 with the JSON on stdout and the Markdown silently dropped, on the one path where
    # this PR's own documented refusal did not run.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    rc = main(["run", path, "--since", "HEAD", "--json", "-", "--report", "step-summary"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--json -" in captured.err and "--report step-summary" in captured.err
    assert captured.out == ""


def test_main_since_no_changes_emits_the_job_summary_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half: with somewhere to write it, the empty run still reports. A CI job that shows
    # survivors on every run gets "nothing to mutate" in the place reviewers look, rather than a
    # blank section indistinguishable from a step that never ran.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--json", "-", "--report", "step-summary"]) == 0
    # Report keys are POSIX-normalized regardless of host OS.
    data = json.loads(capsys.readouterr().out)
    assert data["files"][Path(path).as_posix()]["mutants"] == []
    written = summary.read_text(encoding="utf-8")
    assert "gdmutant: mutation report" in written
    assert "No surviving mutants" in written


def test_main_since_no_changes_without_the_flag_writes_no_job_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ... and only when asked. The env var being set is not a request for a summary.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--json", "-"]) == 0
    assert not summary.exists()


def test_main_since_no_changes_rejects_a_project_dir_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Was: exit 0 with a clean-looking empty report. Both real-run paths exit 2 here, and the guide
    # names a bad --project as an exit-2 cause without qualifying it by path.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    rc = main(["run", path, "--since", "HEAD", "--project", str(tmp_path / "nope"), "--json", "-"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "project directory not found" in captured.err
    assert captured.out == ""  # no report, so nothing looks like a successful empty run


def test_main_since_no_changes_warns_about_a_malformed_ignore_pragma(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Was: the warning blinked in and out depending on whether the diff happened to touch anything.
    # A typo'd pragma is a fact about the file, not about the diff.
    source = "func f(a, b) -> bool:\n\treturn a > b  # gdmutant: ignore[no_such_op]\n"
    path = _repo_with_committed(tmp_path, "f.gd", source)
    assert main(["run", path, "--since", "HEAD", "--json", "-"]) == 0
    assert "names an unknown operator" in capsys.readouterr().err


#: A source whose ignore pragma names an operator that does not exist — the warning is about the
#: file itself, so every path that reads the file owes it to the user regardless of what else is
#: wrong with the invocation.
_BAD_PRAGMA_SRC = "func f(a, b) -> bool:\n\treturn a > b  # gdmutant: ignore[no_such_op]\n"


def test_main_since_no_changes_warns_about_the_pragma_even_with_a_bad_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The combination is the case that got through: the pragma alone was covered, and a bad
    # --project alone was covered, so a path that reported only the second passed both tests. Order
    # decides it — sources are read and warned about before the setup is validated, so one
    # invocation reports everything wrong rather than one thing per round trip.
    path = _repo_with_committed(tmp_path, "f.gd", _BAD_PRAGMA_SRC)
    rc = main(["run", path, "--since", "HEAD", "--project", str(tmp_path / "nope"), "--json", "-"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "names an unknown operator" in err
    assert "project directory not found" in err
    # ... and in that order: the file the user named, then the flag around it.
    assert err.index("names an unknown operator") < err.index("project directory not found")


def test_run_mutation_warns_about_the_pragma_even_with_a_bad_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The reference path, pinned so the ordering the no-change path matches cannot drift out from
    # under it. Without this, a future reorder here would silently make the two disagree again and
    # only the no-change test would fail, pointing at the wrong file.
    path = tmp_path / "f.gd"
    path.write_text(_BAD_PRAGMA_SRC, encoding="utf-8")
    rc = run_mutation(str(path), str(tmp_path / "nope"), MarkerRunner(str(path), ">="))
    assert rc == 2
    err = capsys.readouterr().err
    assert err.index("names an unknown operator") < err.index("project directory not found")


def test_run_mutation_paths_warns_about_the_pragma_even_with_a_bad_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The third path, same reason. All three now agree, and all three are pinned.
    first = tmp_path / "f.gd"
    first.write_text(_BAD_PRAGMA_SRC, encoding="utf-8")
    second = tmp_path / "g.gd"
    second.write_text("func g(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    rc = cli.run_mutation_paths(
        [str(first), str(second)], str(tmp_path / "nope"), MarkerRunner(str(first), ">=")
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert err.index("names an unknown operator") < err.index("project directory not found")


def test_main_since_no_changes_does_not_run_the_require_clean_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A deliberate omission, not an oversight. `--require-clean` guards a file this tool is about to
    # rewrite in place; this path writes no mutant to any file, so refusing here would block a run
    # that carries none of the risk, and the warning it prints ("gdmutant mutates it in place ...")
    # would simply be false. A dirty tree therefore passes, quietly.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--require-clean", "--json", "-"]) == 0
    err = capsys.readouterr().err
    assert "--require-clean" not in err
    assert "mutates it in place" not in err


def test_setup_problem_reports_the_project_dir_before_a_report_target(tmp_path: Path) -> None:
    # Ordering inside the shared preflight: with both wrong, the project dir is named first — it is
    # the one a caller is more likely to have mistyped, and the report target is moot without it.
    problem = cli._setup_problem(str(tmp_path / "nope"), "-", "-", False)
    assert problem is not None
    assert "project directory not found" in problem


def test_setup_problem_is_clean_when_everything_checks_out(tmp_path: Path) -> None:
    assert cli._setup_problem(str(tmp_path), "-", str(tmp_path / "r.html"), False) is None


def test_main_since_no_changes_writes_nothing_to_stdout_without_a_report_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only a caller that asked for a report gets one. Run without `--json`/`--html`, the no-change
    # case stays what it was: the note on stderr, and stdout untouched.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no lines changed since HEAD" in captured.err


def test_main_since_no_changes_under_dry_run_writes_no_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run runs no tests and ignores --json, so the no-change path keeps its plain exit-0 note
    # there rather than emitting a report the flag has already said it would ignore.
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--dry-run", "--json", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no lines changed since HEAD" in captured.err


def test_main_since_no_changes_still_rejects_html_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The no-change path runs the same report-target preflight as a real run, so `--html -` is
    # refused by name here too instead of quietly writing a file called "-".
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "HEAD", "--html", "-"]) == 2
    assert "--html needs a file path" in capsys.readouterr().err


def test_main_since_no_changes_reports_an_unreadable_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Building the empty report still reads each file, so one gdtoolkit cannot parse is the same
    # exit-2 setup error it is on a real run — never a report quietly missing that file.
    path = _repo_with_committed(tmp_path, "f.gd", "func f( ->\n")
    assert main(["run", path, "--since", "HEAD", "--json", "-"]) == 2
    assert "not valid GDScript" in capsys.readouterr().err


def test_main_since_bad_ref_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _repo_with_committed(tmp_path, "f.gd", _TWO_LINE_SRC)
    assert main(["run", path, "--since", "no-such-ref"]) == 2
    assert "git diff for --since no-such-ref failed" in capsys.readouterr().err


def test_run_mutation_with_changed_scopes_the_real_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real (non-dry) path threads `changed` through to the engine via the diff-scoped adapter:
    # only line-2 mutants run, so the report's mutant set is line-2 only.
    path = tmp_path / "f.gd"
    path.write_text(_TWO_LINE_SRC, encoding="utf-8")
    report = tmp_path / "r.json"
    changed = {str(path.resolve()): {2}}
    rc = cli.run_mutation(
        str(path), str(tmp_path), RecordingRunner(), json_path=str(report), changed=changed
    )
    assert rc == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    # Report keys are POSIX-normalized regardless of host OS.
    lines = {m["location"]["start"]["line"] for m in data["files"][path.as_posix()]["mutants"]}
    assert lines == {2}  # only the changed line was mutated


def test_force_utf8_reconfigures_a_stream() -> None:
    """The happy path: a real text stream is switched to UTF-8 with replacement so the CLI's
    Unicode output can't crash on a Windows cp1252 console."""

    class _Stream:
        def __init__(self) -> None:
            self.kwargs: dict[str, str] = {}

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.kwargs = {"encoding": encoding, "errors": errors}

    stream = _Stream()
    cli._force_utf8(stream)
    assert stream.kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_force_utf8_skips_a_stream_without_reconfigure() -> None:
    """A redirected/wrapped stream that has no ``reconfigure`` is a silent no-op, never an error."""
    cli._force_utf8(object())  # no exception


def test_force_utf8_swallows_reconfigure_errors() -> None:
    """A stream that refuses reconfiguration (detached/locked) is left as-is, not fatal — the worst
    case stays the original behaviour, never a new crash."""

    class _Stream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            raise ValueError("stream is detached")

    cli._force_utf8(_Stream())  # no exception


# --- --progress: who is watching, and how often to say something -------------------------------


class _FakeStderr:
    """A stderr stand-in whose `isatty` answer is the thing under test."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:  # pragma: no cover - never written to in these tests
        return len(text)


def _no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "CONTINUOUS_INTEGRATION"):
        monkeypatch.delenv(name, raising=False)


def test_auto_picks_the_terminal_cadence_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci(monkeypatch)
    monkeypatch.setattr(cli.sys, "stderr", _FakeStderr(tty=True))
    assert _resolve_progress_style("auto") is ProgressStyle.RICH


def test_auto_drops_to_the_quiet_cadence_when_stderr_is_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A log file has nobody watching it live, so a 30s heartbeat is just weight in the file.
    _no_ci(monkeypatch)
    monkeypatch.setattr(cli.sys, "stderr", _FakeStderr(tty=False))
    assert _resolve_progress_style("auto") is ProgressStyle.PLAIN


def test_auto_drops_to_the_quiet_cadence_under_ci_even_on_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some CI runners do allocate a TTY, so the TTY test alone is not enough — gdmutant ships a
    # GitHub Action, which is exactly that case.
    monkeypatch.setattr(cli.sys, "stderr", _FakeStderr(tty=True))
    monkeypatch.setenv("CI", "true")
    assert _resolve_progress_style("auto") is ProgressStyle.PLAIN


def test_ci_detection_wants_the_exact_string_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # Infection's test, and the reason it is the right one: `CI=false` is set by real systems, and
    # treating mere presence as truth would misread it.
    monkeypatch.setattr(cli.sys, "stderr", _FakeStderr(tty=True))
    _no_ci(monkeypatch)
    monkeypatch.setenv("CI", "false")
    assert _resolve_progress_style("auto") is ProgressStyle.RICH
    monkeypatch.setenv("CONTINUOUS_INTEGRATION", "true")
    assert _resolve_progress_style("auto") is ProgressStyle.PLAIN


def test_auto_treats_a_stderr_without_isatty_as_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest's own capture object has no `isatty` at some versions, and a caller may replace stderr
    # with any file-like object. Missing the method means "not a terminal", never a crash.
    _no_ci(monkeypatch)
    monkeypatch.setattr(cli.sys, "stderr", object())
    assert _resolve_progress_style("auto") is ProgressStyle.PLAIN


def test_explicit_choices_override_the_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci(monkeypatch)
    monkeypatch.setattr(cli.sys, "stderr", _FakeStderr(tty=True))
    assert _resolve_progress_style("plain") is ProgressStyle.PLAIN
    assert _resolve_progress_style("none") is ProgressStyle.NONE


def test_main_threads_the_resolved_progress_style_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gd(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_mutation(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: RecordingRunner())
    monkeypatch.setattr(cli, "run_mutation", fake_run_mutation)
    assert (
        main(
            [
                "run",
                str(path),
                "--project",
                str(tmp_path),
                "--runner",
                "gdunit4",
                "--progress",
                "none",
            ]
        )
        == 0
    )
    assert captured["progress_style"] is ProgressStyle.NONE


def test_progress_defaults_to_auto_in_the_parser() -> None:
    args = build_parser().parse_args(["run", "x.gd"])
    assert args.progress_style == "auto"


def test_none_gets_no_progress_emitter_at_all() -> None:
    # `ProgressStyle` governs the periodic heartbeat only — the plan line, the per-mutant lines and
    # the closing wall-clock reach the emitter whatever the style is. Withholding the emitter is
    # what makes `--progress none` mean no progress rather than a tenth less of it.
    assert cli._progress_emitter(ProgressStyle.NONE) is None


def test_every_other_style_emits_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    for style in (ProgressStyle.RICH, ProgressStyle.PLAIN):
        emit = cli._progress_emitter(style)
        assert emit is not None
        emit(f"beat {style.value}")
    captured = capsys.readouterr()
    assert "beat rich" in captured.err and "beat plain" in captured.err
    assert captured.out == ""  # never stdout: that channel belongs to the --json - report


def test_progress_none_prints_no_progress_during_a_real_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The measured defect: `--progress none` still printed two lines per mutant (one "running", one
    # verdict), 36 of them on an 18-mutant file. Silence now means silence — while the summary and
    # the survivors, which are the run's result rather than progress about it, are untouched.
    path = _gd(tmp_path)  # 3 mutants
    rc = run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), ">="),
        progress_style=ProgressStyle.NONE,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "[1/3]" not in captured.err  # no per-mutant line
    assert "mutants to run" not in captured.err  # no opening plan line
    assert "Done in" not in captured.err  # no closing wall-clock
    # The baseline notices go too, and that is the deliberate part: they are the anti-"looks hung"
    # signal, so a carve-out for them is exactly the "none means almost none" ambiguity this fix
    # exists to remove. Somebody watching a terminal uses auto or plain.
    assert "baseline" not in captured.err
    assert "Mutation score:" in captured.out  # the result itself still prints


def test_the_default_style_still_prints_the_plan_and_closing_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of the pair: only `none` is silent. Without this, silencing every style would
    # pass the test above.
    path = _gd(tmp_path)
    run_mutation(
        str(path),
        str(tmp_path),
        MarkerRunner(str(path), ">="),
        progress_style=ProgressStyle.RICH,
    )
    err = capsys.readouterr().err
    assert "mutants to run" in err
    assert "Done in" in err
    assert "running the unmutated (baseline) suite" in err  # the notice `none` deliberately drops


def test_progress_none_is_silent_on_the_multi_file_path_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `run_mutation_paths` builds its own emitter, so the directory path needs its own guard: a fix
    # applied to only one of the two call sites would pass every single-file test above.
    first = _gd(tmp_path)
    second = tmp_path / "g.gd"
    second.write_text("func g(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    rc = cli.run_mutation_paths(
        [str(first), str(second)],
        str(tmp_path),
        MarkerRunner(str(first), ">="),
        progress_style=ProgressStyle.NONE,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "mutants to run" not in err
    assert "Done in" not in err
    assert "mutating" not in err


# --- .gdmutant.toml may not decide what gets executed --------------------------------------
#
# The config file is read from the directory gdmutant is run in, so on a cloned project it is a
# file somebody else wrote. Two of its keys name a program gdmutant then executes: `command` goes
# straight to the operating system, and `godot` is the binary every JUnit runner launches. Acting
# on either without being asked turns "point the mutation tester at this checkout" into "run
# whatever this checkout says". If the file sets either key at all, gdmutant refuses to run unless
# the user adds --trust-config — no exception for also passing the same key as a flag, which
# never actually helped the person it should have (a project's own config setting one of these
# once, so nobody retypes it, still needs --trust-config every time either way).


def _record_launches(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every program gdmutant tries to launch, minus its own git status checks.

    `subprocess.run` still answers, so the surrounding code behaves normally: the stand-in reports
    the exit code git uses outside a work tree, which is what these throwaway directories are.
    """
    launched: list[list[str]] = []

    def spy(
        argv: Sequence[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if list(argv)[:1] != ["git"]:
            launched.append(list(argv))
        return subprocess.CompletedProcess(list(argv), returncode=128, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", spy)
    return launched


def _payload_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    """A project whose `.gdmutant.toml` carries `body`; returns the source path to run against.

    Points `_CONFIG_FILENAME` at the file and hands back an absolute source path, rather than
    chdir()-ing into the directory and naming `player.gd` relative to it. Same reason the two
    config tests further up give: a global cwd change breaks mutmut's stats collection, which
    resolves `source_paths=["gdmutant"]` against the working directory -- so the dogfood baseline
    aborts and the mutation score stops existing rather than going down. What these tests are
    about is the config file and the argv; neither cares where the process is standing.
    """
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text(body, encoding="utf-8")
    source = tmp_path / "player.gd"
    source.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    return str(source)


def test_a_config_supplied_command_is_refused_and_never_reaches_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The plain local case: clone a project, run gdmutant in it, and its config file picks both
    # the runner and the command string. Before this was refused, that command ran -- verified by
    # a payload that wrote a file. Nothing may reach a subprocess here.
    source = _payload_config(tmp_path, monkeypatch, 'runner = "command"\ncommand = "touch pwned"\n')
    launched = _record_launches(monkeypatch)

    rc = cli.main(["run", source])

    # Asserted first: whether the payload ran is the whole point, and it must be what fails.
    assert launched == [], "the config's command must never be executed"
    assert rc == 2
    err = capsys.readouterr().err
    assert "sets 'command'" in err
    assert "--trust-config" in err


def test_a_config_supplied_godot_binary_is_refused_even_behind_an_explicit_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The one that reaches CI. The published action always passes --runner explicitly, which
    # overrides a config `runner` -- but it never passes --godot, so a config `godot` key survived
    # and named the binary the JUnit runner launches. A repository could therefore choose what ran
    # on the machine testing it. This mirrors the action's flag shape exactly.
    source = _payload_config(tmp_path, monkeypatch, 'godot = "./payload"\n')
    launched = _record_launches(monkeypatch)

    rc = cli.main(["run", source, "--project", str(tmp_path), "--runner", "gdunit4"])

    assert launched == [], "the config's godot binary must never be launched"
    assert rc == 2
    assert "sets 'godot'" in capsys.readouterr().err


def test_a_config_supplied_project_is_refused_alone_with_no_trust_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `project` alone, with neither `command` nor `godot` in the file: the gate must fire on
    # `project` by itself, not only when it happens to accompany one of the other two keys.
    proj = tmp_path / "elsewhere"
    proj.mkdir()
    source = _payload_config(tmp_path, monkeypatch, f"project = '{proj}'\n")

    rc = cli.main(["run", source])

    assert rc == 2
    assert "sets 'project'" in capsys.readouterr().err


def test_the_refusal_names_every_program_key_the_file_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both keys at once: the message has to list both, or fixing one leaves the user stuck on the
    # other with no idea why.
    source = _payload_config(tmp_path, monkeypatch, 'command = "payload"\ngodot = "./payload"\n')

    assert cli.main(["run", source]) == 2

    assert "sets 'command' and 'godot'" in capsys.readouterr().err


def test_trust_config_lets_a_project_use_its_own_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The legitimate use has to keep working: a project's own maintainers put `command` in the
    # file precisely so they need not retype it. --trust-config is how they say the file is theirs.
    source = _payload_config(
        tmp_path, monkeypatch, 'runner = "command"\ncommand = "my-test-harness --headless"\n'
    )
    captured: dict[str, object] = {}

    def record(source: str, project: str, runner: object, **kw: object) -> int:
        captured["r"] = runner
        return 0

    monkeypatch.setattr(cli, "run_mutation", record)

    assert cli.main(["run", source, "--trust-config"]) == 0

    assert isinstance(captured["r"], CommandRunner)
    assert list(captured["r"].command) == ["my-test-harness", "--headless"]


def test_naming_the_program_on_the_command_line_still_needs_the_trust_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The file names a program at all is what's refused -- not "and the two disagree". An earlier
    # version let an explicit --godot skip the flag when it happened to match nothing in
    # particular; that carve-out never helped the one person it should have (a project's own
    # config setting `godot` once so nobody retypes it still needed --trust-config every time
    # anyway), so it was removed rather than kept as an unused exception.
    source = _payload_config(tmp_path, monkeypatch, 'godot = "./payload"\n')
    launched = _record_launches(monkeypatch)

    rc = cli.main(["run", source, "--godot", "/my/own/godot"])

    assert launched == []
    assert rc == 2
    assert "sets 'godot'" in capsys.readouterr().err


def test_a_config_that_names_no_program_still_needs_no_trust_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The refusal is narrow on purpose. Every other key is inert -- a directory, a glob, a number,
    # a res:// path -- and must keep working untouched, or the fix costs far more than it buys.
    source = _payload_config(
        tmp_path,
        monkeypatch,
        'runner = "gut"\ntests = "res://test/unit"\ntimeout = 30\nexclude = ["*_gen.gd"]\n',
    )
    captured: dict[str, object] = {}

    def record(source: str, project: str, runner: object, **kw: object) -> int:
        captured["r"] = runner
        return 0

    monkeypatch.setattr(cli, "run_mutation", record)

    assert cli.main(["run", source]) == 0

    assert isinstance(captured["r"], GutRunner)
    assert captured["r"].test_dir == "res://test/unit"


def test_the_trust_flag_cannot_itself_come_from_the_config_file(tmp_path: Path) -> None:
    # The obvious way to defeat all of this would be for the file to grant itself the trust. It
    # cannot: `trust-config` is not a recognised key, so it warns like any other typo and is
    # dropped, and the flag stays something only the command line can set.
    cfg = tmp_path / ".gdmutant.toml"
    cfg.write_text('trust-config = true\ncommand = "payload"\n', encoding="utf-8")

    settings = cli._load_config(cfg)

    assert settings is not None
    assert "trust_config" not in settings


# --- --require-clean refuses whatever it could not confirm ----------------------------------
#
# The flag is someone asking for a guarantee before gdmutant edits their file in place: don't
# start unless git could put this back. The check answered that question with a plain
# `git status --porcelain`, which is silent in three quite different situations -- the file is
# genuinely committed and unmodified, git could not be run, or the file is ignored and git has
# never held a copy of it. Only the first is safe, and all three passed.


def test_a_gitignored_source_is_not_treated_as_safely_committed(tmp_path: Path) -> None:
    # The sharpest case, because it is the one where git has NO copy at all -- not an out-of-date
    # one, none -- and it was reported exactly like a clean, committed file. `git status
    # --porcelain` says nothing about ignored paths unless asked.
    repo = _committed_repo(tmp_path)
    (repo / ".gitignore").write_text("generated.gd\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated")
    ignored = repo / "generated.gd"
    ignored.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")

    backup = _git_backup(str(ignored))

    assert backup.backed_up is False, "an ignored file has no copy in git and must not read clean"
    assert "ignored by git" in backup.reason


def test_require_clean_refuses_a_gitignored_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ...and the flag has to act on it. Running here would edit in place a file that no checkout
    # could ever restore.
    repo = _committed_repo(tmp_path)
    (repo / ".gitignore").write_text("generated.gd\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated")
    ignored = repo / "generated.gd"
    ignored.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")

    rc = run_mutation(str(ignored), str(repo), RunBoomRunner(), require_clean=True)

    assert rc == 2
    assert "ignored by git" in capsys.readouterr().err


def test_require_clean_refuses_when_the_file_is_not_in_a_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Fail closed. Passing here returned the flag's assurance without ever having checked
    # anything: there is no repository, so nothing could be recovered from one.
    path = _gd(tmp_path)

    rc = run_mutation(str(path), str(tmp_path), RunBoomRunner(), require_clean=True)

    assert rc == 2
    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert "--no-require-clean" in err, "the refusal must say how to proceed anyway"


def test_require_clean_refuses_when_git_cannot_be_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same rule for a missing git binary: "could not check" is not "checked and safe".
    repo = _committed_repo(tmp_path)

    def no_git(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cli.subprocess, "run", no_git)
    rc = run_mutation(str(repo / "f.gd"), str(repo), RunBoomRunner(), require_clean=True)

    assert rc == 2
    assert "may not be installed" in capsys.readouterr().err


def test_without_the_flag_an_unjudgeable_tree_stays_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The default must not change: gdmutant has to work outside git at all, so a file it cannot
    # judge is not worth a warning on every single run. Only the flag turns not-knowing into a
    # refusal, because only the flag promised anything.
    path = _gd(tmp_path)

    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), "a >= b"))

    assert rc == 0
    assert "warning:" not in capsys.readouterr().err


def test_multi_file_require_clean_refuses_an_unjudgeable_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The multi-file path has its own copy of the decision, so it needs its own proof.
    first = _gd(tmp_path)  # f.gd
    second = tmp_path / "g.gd"
    second.write_text(_gd(tmp_path).read_text(encoding="utf-8"), encoding="utf-8")

    rc = cli.run_mutation_paths(
        [str(first), str(second)], str(tmp_path), RunBoomRunner(), require_clean=True
    )

    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err


# --- --jobs with a source outside --project exits cleanly, not with a traceback --------------


def _outside_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project directory, and a .gd file that is its sibling rather than inside it."""
    project = tmp_path / "godot-project"
    project.mkdir()
    outside = _gd(tmp_path)  # f.gd, next to the project rather than in it
    return project, outside


def test_jobs_with_a_source_outside_the_project_exits_two_with_an_explanation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The engine refuses to write outside a worker's copy. The CLI has to turn that into the same
    # exit 2 every other setup mistake gets, with a message -- not a raw traceback.
    project, outside = _outside_project(tmp_path)

    rc = run_mutation(str(outside), str(project), MarkerRunner(str(outside), "zzz"), jobs=2)

    assert rc == 2
    err = capsys.readouterr().err
    assert "is not inside the project directory" in err
    assert "Traceback" not in err


def test_jobs_auto_with_a_source_outside_the_project_falls_back_to_serial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --jobs auto (the default) picks its own worker count, so a layout an explicit --jobs N would
    # reject outright (a source outside --project, which the engine can't isolate for a worker) must
    # not surface as an error here — that's the exact documented "point it at your own project"
    # layout in the README. It should just run serially, silently, with a real result.
    project, outside = _outside_project(tmp_path)

    rc = run_mutation(
        str(outside), str(project), MarkerRunner(str(outside), "zzz"), jobs=2, jobs_auto=True
    )

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "is not inside the project directory" not in combined
    assert "Traceback" not in combined
    assert "Running" not in combined  # the announced plan matches what actually ran: serial


def test_multi_file_jobs_with_a_source_outside_the_project_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The multi-file path builds its own run, so it needs the same guard proved separately.
    project, outside = _outside_project(tmp_path)
    second = tmp_path / "g.gd"
    second.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")

    rc = cli.run_mutation_paths(
        [str(outside), str(second)], str(project), MarkerRunner(str(outside), "zzz"), jobs=2
    )

    assert rc == 2
    assert "is not inside the project directory" in capsys.readouterr().err


# --- The check has to describe the file whose bytes actually change -------------------------


def test_a_symlinked_source_is_judged_by_the_file_it_points_at(tmp_path: Path) -> None:
    # Git stores a symlink as the link string, not as the content it points at. So a committed,
    # unmodified link read as safely backed up -- while the bytes gdmutant rewrites are the
    # target's, which here live outside the repository entirely and have no copy anywhere. That is
    # --require-clean handing back exactly the guarantee it exists to withhold.
    (tmp_path / "repo").mkdir()
    repo = _committed_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "real.gd"
    target.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    link = repo / "link.gd"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")
    _git(repo, "add", "link.gd")
    _git(repo, "commit", "-m", "commit the link itself")

    backup = _git_backup(str(link))

    assert backup.backed_up is not True, "the link's own cleanliness says nothing about the target"


def test_a_symlink_to_a_tracked_file_in_the_same_repo_is_still_safe(tmp_path: Path) -> None:
    # The other direction, so the fix is not just "refuse every symlink": a link whose target is
    # committed and clean in the same repository really is recoverable, and must stay usable.
    repo = _committed_repo(tmp_path)
    link = repo / "alias.gd"
    try:
        link.symlink_to(repo / "f.gd")
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")

    assert _git_backup(str(link)).backed_up is True


# --- A git refusal keeps git's own explanation ------------------------------------------------


def test_a_git_failure_that_is_not_a_missing_repo_keeps_gits_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything non-zero used to collapse into "not inside a git working tree". Dubious ownership
    # is the case that matters: the repository is right there, git prints the exact command that
    # fixes it, and the old message sent the user hunting for a repo they already have.
    path = _gd(tmp_path)
    real_run = subprocess.run

    def dubious_ownership(argv: Sequence[str], **kwargs: object) -> object:
        if list(argv)[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(
                list(argv),
                128,
                stdout="",
                stderr="fatal: detected dubious ownership in repository at '/repo'\n"
                "To add an exception: git config --global --add safe.directory /repo\n",
            )
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli.subprocess, "run", dubious_ownership)
    backup = _git_backup(str(path))

    assert backup.backed_up is None
    assert "dubious ownership" in backup.reason
    assert "safe.directory" in backup.reason, "git's own fix hint must survive"


def test_a_silent_git_failure_still_says_something(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-zero exit with nothing on stderr must not produce a dangling "git could not check X — ".
    path = _gd(tmp_path)
    real_run = subprocess.run

    def mute_failure(argv: Sequence[str], **kwargs: object) -> object:
        if list(argv)[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(list(argv), 128, stdout="", stderr="   \n")
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli.subprocess, "run", mute_failure)
    assert "said nothing about why" in _git_backup(str(path)).reason


# --- Every message names the file git was actually asked about ---------------------------------


def _link_to_a_file_outside_every_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A committed symlink inside a real repository, pointing at a file no repository covers."""
    (tmp_path / "repo").mkdir()
    repo = _committed_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "real.gd"
    target.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    link = repo / "link.gd"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")
    _git(repo, "add", "link.gd")
    _git(repo, "commit", "-m", "commit the link itself")
    return link, target


def test_a_git_failure_on_a_symlink_names_the_file_git_was_asked_about(tmp_path: Path) -> None:
    # git runs in the resolved target's directory, which here has no repository above it. Reporting
    # "not a git repository" against the link's own path describes a file that unambiguously IS in
    # one, sitting right where the user can see it -- the same hunt for a repository they already
    # have that keeping git's own words exists to end.
    link, target = _link_to_a_file_outside_every_repo(tmp_path)

    backup = _git_backup(str(link))

    assert backup.backed_up is None
    assert "not a git repository" in backup.reason
    assert os.path.realpath(str(target)) in backup.reason


def test_an_ignored_symlink_target_is_named_in_the_message(tmp_path: Path) -> None:
    # The ignored and dirty messages judge the target too, so they need the same naming. Less
    # misleading than the git-failure case, and wrong in the same way: the advice is about a file
    # the message does not name.
    (tmp_path / "repo").mkdir()
    repo = _committed_repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    _committed_repo(other)
    (other / "generated.gd").write_text("var x := 1\n", encoding="utf-8")
    (other / ".gitignore").write_text("generated.gd\n", encoding="utf-8")
    _git(other, "add", ".gitignore")
    _git(other, "commit", "-m", "ignore generated.gd")
    link = repo / "link.gd"
    try:
        link.symlink_to(other / "generated.gd")
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")

    backup = _git_backup(str(link))

    assert backup.backed_up is False
    assert "is ignored by git" in backup.reason
    assert os.path.realpath(str(other / "generated.gd")) in backup.reason


def test_a_dirty_symlink_target_is_named_in_the_message(tmp_path: Path) -> None:
    # The third message, for completeness: "commit or stash first" is unfollowable advice when the
    # file it names is the clean link rather than the dirty file the advice is really about.
    (tmp_path / "repo").mkdir()
    repo = _committed_repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    _committed_repo(other)
    (other / "f.gd").write_text("var changed := 2\n", encoding="utf-8")
    link = repo / "link.gd"
    try:
        link.symlink_to(other / "f.gd")
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")

    backup = _git_backup(str(link))

    assert backup.backed_up is False
    assert "has uncommitted changes" in backup.reason
    assert os.path.realpath(str(other / "f.gd")) in backup.reason


def test_an_ordinary_file_is_named_once_and_only_once() -> None:
    # The note is for a source whose bytes live somewhere else. A file that is its own target must
    # not collect it: a message that says "(resolved to ...)" about a path the user typed reads as
    # gdmutant having found something, when it found nothing. Asking `Path.absolute()` instead of
    # `os.path.abspath` is what puts it there -- `absolute()` only prefixes the working directory
    # and never follows a link, so every file under a symlinked DIRECTORY looks moved to it.
    #
    # `os.path.relpath` (used below to build a relative-path test input, not under test itself)
    # defaults its `start` to the CWD, and raises `ValueError` if that CWD is on a different drive
    # than the file -- exactly gdmutant's own `--jobs` isolation-copy check works around in
    # `engine/loop.py`. A hosted Windows runner's checkout and pytest's `tmp_path` routinely land on
    # different drives (checkout on `D:`, `%TEMP%` on `C:`); a local dev machine usually has both on
    # `C:`, which is why this only ever broke in CI. `tmp_path` is deliberately not used here:
    # `tempfile.TemporaryDirectory(dir=".")` creates the scratch directory *inside the repo checkout
    # itself*, same drive as the cwd by construction, without ever changing the process's actual
    # working directory -- chdir is banned repo-wide (see
    # test_mutation_baseline_inputs.py::test_no_test_moves_the_process_to_another_directory) because
    # mutmut resolves `source_paths=['gdmutant']` against the cwd and a stray chdir aborts its
    # baseline collection.
    with tempfile.TemporaryDirectory(dir=".") as scratch:
        path = _gd(Path(scratch))

        assert cli._judged_path(str(path)) == str(path)
        assert cli._judged_path(os.path.relpath(str(path))) == os.path.relpath(str(path))


def test_a_path_typed_in_a_different_case_is_not_read_as_a_different_file(tmp_path: Path) -> None:
    # Windows is a deployment target here, and `os.path.realpath` returns a name the way the
    # filesystem spells it. So `c:\project\player.gd` resolves to `C:\Project\player.gd`, and a raw
    # string comparison reads that as a move. `os.path.normcase` is what keeps the note off it, and
    # is a no-op on the platforms where case really does distinguish two files.
    path = _gd(tmp_path)
    retyped = str(path).swapcase() if os.path.normcase("A") == os.path.normcase("a") else str(path)

    assert cli._judged_path(retyped) == retyped


# --- The advice has to be something the user can actually do ----------------------------------


def test_a_gitignored_file_warns_by_default_with_advice_that_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # This IS a change to the default: the old check was silent here, because plain `git status`
    # never mentions an ignored file. Warning is right -- it is the one case gdmutant can
    # positively tell has no copy anywhere -- but "commit or stash first" is advice git will
    # refuse to carry out on an ignored file, so it must not say that.
    repo = _committed_repo(tmp_path)
    (repo / ".gitignore").write_text("generated.gd\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated")
    ignored = repo / "generated.gd"
    ignored.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")

    rc = run_mutation(str(ignored), str(repo), MarkerRunner(str(ignored), "a >= b"))

    assert rc == 0, "the default mode warns, it does not refuse"
    err = capsys.readouterr().err
    assert "is ignored by git" in err
    assert "Take it out of .gitignore" in err
    assert "Commit or stash" not in err, "you cannot commit a file git is ignoring"


# --- `gdmutant example`: no project of your own yet, so the tool ships one -----------------------


def test_example_with_no_destination_defaults_to_the_bundled_name_in_the_cwd() -> None:
    # A pure path computation, deliberately not exercised by writing a file into the real process
    # cwd: this suite must never chdir (it would break mutmut's `source_paths=['gdmutant']` stats
    # collection), and a real write here would land in the repo checkout itself.
    assert cli._example_target(None) == Path(_EXAMPLE_NAME)


def test_example_writes_the_bundled_file_and_reports_where(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / _EXAMPLE_NAME
    rc = _write_example(str(target))
    assert rc == 0
    assert target.is_file()
    assert "clamp_initiative" in target.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert str(target) in out
    assert "--dry-run" in out


def test_example_written_file_is_real_gdscript_dry_run_can_list(tmp_path: Path) -> None:
    # Not just "some bytes landed" -- prove the bundled source is valid, mutable GDScript by
    # actually running the tool's own --dry-run preview on it.
    target = tmp_path / "hello.gd"
    assert _write_example(str(target)) == 0
    assert list_mutants(str(target)) == 0


def test_example_accepts_an_explicit_destination_path(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "mine.gd"
    target.parent.mkdir()
    assert _write_example(str(target)) == 0
    assert target.is_file()


def test_example_given_a_directory_writes_the_default_name_inside_it(tmp_path: Path) -> None:
    assert _write_example(str(tmp_path)) == 0
    assert (tmp_path / _EXAMPLE_NAME).is_file()


def test_main_example_dispatches_to_write_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / _EXAMPLE_NAME
    rc = main(["example", str(target)])
    assert rc == 0
    assert target.is_file()


def test_example_unwritable_destination_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same late-write backstop as the report writers: the target's parent is a regular file, not a
    # directory, so the write itself fails -- exit 2 with a message, not an uncaught OSError.
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("x", encoding="utf-8")
    bad_target = not_a_dir / _EXAMPLE_NAME

    rc = _write_example(str(bad_target))

    assert rc == 2
    assert "cannot write" in capsys.readouterr().err


def test_example_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / _EXAMPLE_NAME
    target.write_text("something the caller already had\n", encoding="utf-8")

    rc = _write_example(str(target))

    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    # Untouched -- a silent overwrite is exactly the failure this guard exists to prevent.
    assert target.read_text(encoding="utf-8") == "something the caller already had\n"


# --- `gdmutant init`: scaffold a starter .gdmutant.toml, so nobody hand-writes the first one ------


def test_detect_runner_prefers_gdunit4_when_both_addons_present(tmp_path: Path) -> None:
    (tmp_path / _GDUNIT_ADDON_REL).mkdir(parents=True)
    (tmp_path / _GUT_ADDON_REL).mkdir(parents=True)
    assert _detect_runner(tmp_path) == "gdunit4"


def test_detect_runner_finds_gut_when_only_gut_is_installed(tmp_path: Path) -> None:
    (tmp_path / _GUT_ADDON_REL).mkdir(parents=True)
    assert _detect_runner(tmp_path) == "gut"


def test_detect_runner_returns_none_with_no_addon_installed(tmp_path: Path) -> None:
    assert _detect_runner(tmp_path) is None


def test_write_init_config_creates_a_file_of_only_valid_keys(tmp_path: Path) -> None:
    import tomllib

    rc = _write_init_config(tmp_path)

    assert rc == 0
    cfg = tmp_path / _CONFIG_FILENAME
    assert cfg.is_file()
    keys = set(tomllib.loads(cfg.read_text(encoding="utf-8")))
    assert keys, "the scaffold must not be empty"
    assert keys <= set(_CONFIG_KEY_TO_DEST), f"unknown keys the loader would reject: {keys}"


def test_write_init_config_detects_and_sets_the_runner(tmp_path: Path) -> None:
    import tomllib

    (tmp_path / _GDUNIT_ADDON_REL).mkdir(parents=True)

    assert _write_init_config(tmp_path) == 0

    cfg = tmp_path / _CONFIG_FILENAME
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["runner"] == "gdunit4"


def test_write_init_config_leaves_runner_unset_when_undetected(tmp_path: Path) -> None:
    import tomllib

    assert _write_init_config(tmp_path) == 0

    cfg = tmp_path / _CONFIG_FILENAME
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert "runner" not in parsed  # nothing to guess from -- left commented, not invented


def test_write_init_config_scaffold_is_readable_by_load_config(tmp_path: Path) -> None:
    # The file init writes must be exactly what the loader accepts -- not just parseable TOML, but
    # a config _load_config validates cleanly, with nothing trust-required set (init never guesses
    # project/command/godot).
    (tmp_path / _GUT_ADDON_REL).mkdir(parents=True)
    assert _write_init_config(tmp_path) == 0

    settings = _load_config(tmp_path / _CONFIG_FILENAME)
    assert settings is not None
    assert settings == {"runner": "gut", "tests": "res://test"}


def test_write_init_config_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / _CONFIG_FILENAME
    cfg.write_text("runner = 'command'\n", encoding="utf-8")

    rc = _write_init_config(tmp_path)

    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    assert cfg.read_text(encoding="utf-8") == "runner = 'command'\n"  # untouched


def test_write_init_config_force_overwrites_an_existing_file(tmp_path: Path) -> None:
    cfg = tmp_path / _CONFIG_FILENAME
    cfg.write_text("runner = 'command'\n", encoding="utf-8")

    rc = _write_init_config(tmp_path, force=True)

    assert rc == 0
    assert "runner = 'command'" not in cfg.read_text(encoding="utf-8")


def test_write_init_config_unwritable_destination_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("x", encoding="utf-8")

    rc = _write_init_config(not_a_dir)

    assert rc == 2
    assert "cannot write" in capsys.readouterr().err


def test_main_init_dispatches_to_write_init_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No addons/ next to the repo checkout's cwd, so this exercises the undetected-runner path too.
    cfg = tmp_path / _CONFIG_FILENAME
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))

    assert main(["init"]) == 0

    assert cfg.is_file()
    assert "Wrote" in capsys.readouterr().out


def test_main_init_a_second_time_refuses_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / _CONFIG_FILENAME
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))

    assert main(["init"]) == 0
    before = cfg.read_bytes()
    assert main(["init"]) == 2
    assert cfg.read_bytes() == before


def test_main_init_force_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / _CONFIG_FILENAME
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))

    assert main(["init"]) == 0
    assert main(["init", "--force"]) == 0


def test_main_init_force_recovers_from_a_malformed_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The main reason to reach for `init --force` in the first place: the existing config is broken
    # enough that you want to regenerate it. `main()` used to call `_load_config()` unconditionally
    # before dispatch, so a `.gdmutant.toml` that fails to parse returned 2 right there -- before
    # `init`'s own dispatch, `--force` included, was ever reached.
    cfg = tmp_path / _CONFIG_FILENAME
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    cfg.write_text("this is not valid toml [[[", encoding="utf-8")

    assert main(["init", "--force"]) == 0
    assert "this is not valid toml" not in cfg.read_text(encoding="utf-8")


def test_main_run_subcommand_is_unaffected_by_the_init_subparser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adding the `init` subparser must leave `run`'s own behaviour and defaults untouched.
    path = _gd(tmp_path)
    cfg = tmp_path / "no-such-config.toml"  # absent -- _load_config returns {} either way
    monkeypatch.setattr(cli, "_CONFIG_FILENAME", str(cfg))
    runner = RecordingRunner()
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: runner)

    assert main(["run", str(path), "--runner", "gdunit4"]) == 0
