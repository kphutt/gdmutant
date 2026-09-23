"""The "no coverage" verdict on every surface that reports verdicts (docs/decisions/0017, step 2).

These surfaces must agree, so each one is pinned here side by side: the console summary, the JSON
report, the HTML report, the Markdown job summary, the all-survived warning, and the CLI that wires
the option through both of its run paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gdmutant import cli
from gdmutant.adapters.gdscript.marker_run import GDScriptMarker
from gdmutant.cli import main
from gdmutant.engine.coverage import CoverageAnalysis
from gdmutant.engine.htmlreport import render_html, report_view
from gdmutant.engine.loop import CoverageRunFailed, MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import (
    NO_COVERAGE_GAP,
    NO_COVERAGE_REASON,
    all_survived_warning,
    console_summary,
    job_summary_markdown,
    stryker_report,
)
from gdmutant.engine.runner import SuiteResult
from gdmutant.engine.spans import Span

_SRC = "func f(a, b):\n\treturn a > b\n\treturn a + b\n"
_KILLED = Mutant("f.gd", Span(2, 11, 2, 12), "comparison", ">", ">=")
_SURVIVED = Mutant("f.gd", Span(2, 11, 2, 12), "comparison", ">", "<")
_UNCOVERED = Mutant("f.gd", Span(3, 11, 3, 12), "arithmetic", "+", "-")


def _run(*, coverage: bool = True, checked: bool = True) -> MutationRun:
    return MutationRun(
        (
            MutantOutcome(_KILLED, Verdict.KILLED),
            MutantOutcome(_SURVIVED, Verdict.SURVIVED),
            MutantOutcome(_UNCOVERED, Verdict.NO_COVERAGE, self_checked=checked),
        ),
        coverage_analysis=coverage,
    )


def test_the_console_lists_no_coverage_mutants_under_their_own_label() -> None:
    text = console_summary(_run())
    assert "No coverage (1): no test reaches these lines, so start with a test that runs them." in (
        text
    )
    assert "  f.gd:3:11  arithmetic  + -> -" in text
    assert "Survivors (1):" in text
    assert "Mutation score: 33.3%" in text
    assert "  no coverage: 1  (no test reaches it, scored as survived)" in text
    assert text.endswith(
        "Coverage self-check: re-ran 1 of the 1 no-coverage mutants against the whole suite, and "
        "every one survived there, as the map said."
    )
    assert text.index("No coverage (1)") < text.index("Results")


def test_the_console_says_the_self_check_compared_nothing_when_nothing_was_uncovered() -> None:
    run = MutationRun((MutantOutcome(_KILLED, Verdict.KILLED),), coverage_analysis=True)
    text = console_summary(run)
    assert "  no coverage: 0  " in text
    assert text.endswith(
        "Coverage self-check: compared 0 mutants, because nothing was decided from the "
        "coverage map."
    )
    assert "No coverage (" not in text


def test_the_console_says_nothing_new_with_coverage_analysis_off() -> None:
    run = MutationRun((MutantOutcome(_KILLED, Verdict.KILLED),))
    text = console_summary(run)
    assert "no coverage" not in text.lower()
    assert "self-check" not in text


def test_a_no_coverage_count_is_never_hidden_even_if_the_run_forgot_the_flag() -> None:
    # Two paths that should agree: a caller that aggregates outcomes but drops the flag must still
    # show the count, since the score already includes it.
    text = console_summary(_run(coverage=False))
    assert "  no coverage: 1  " in text
    assert "Coverage self-check" not in text
    assert "**1 no coverage**" in job_summary_markdown(_run(coverage=False))


def test_the_json_status_is_the_schemas_no_coverage_with_its_own_narrative() -> None:
    report = stryker_report(_run(), "f.gd", _SRC, "gdscript")
    mutants = report["files"]["f.gd"]["mutants"]
    assert [m["status"] for m in mutants] == ["Killed", "Survived", "NoCoverage"]
    uncovered = mutants[2]
    assert uncovered["description"] == NO_COVERAGE_GAP
    assert uncovered["statusReason"] == NO_COVERAGE_REASON
    assert "\n\n" in NO_COVERAGE_REASON  # risk, then where to start, as the page splits it
    assert mutants[1]["description"] != NO_COVERAGE_GAP


def test_the_html_labels_it_counts_it_and_scores_it_like_the_console() -> None:
    report = stryker_report(_run(), "f.gd", _SRC, "gdscript")
    view = report_view(report)
    assert view.no_coverage == 1
    assert view.survived == 1
    assert view.score == 33.3  # the console's 33.3%, not the 50% that leaving it out would give
    assert ("no coverage", 1, "NoCoverage") in view.rare
    (file_view,) = view.files
    assert file_view.no_coverage == 1
    assert file_view.score == 33.3
    finding = next(f for f in file_view.findings if f.line == 3)
    (angle,) = finding.angles
    assert (angle.tag, angle.cls) == ("no coverage", "sv")
    assert angle.outcome == "no test reaches this line, so none could fail"
    assert finding.rare == ["NoCoverage"]
    assert finding.gap == NO_COVERAGE_GAP
    page = render_html(report)
    assert "1 of 3 caught" in page
    assert 'data-filter="rare:NoCoverage"' in page


def test_the_html_is_unchanged_without_no_coverage_mutants() -> None:
    report = stryker_report(
        MutationRun((MutantOutcome(_KILLED, Verdict.KILLED),)), "f.gd", _SRC, "gdscript"
    )
    view = report_view(report)
    assert view.no_coverage == 0
    assert all(status != "NoCoverage" for _, _, status in view.rare)
    assert "1 of 1 caught" in render_html(report)


def test_the_job_summary_lists_no_coverage_mutants() -> None:
    markdown = job_summary_markdown(_run())
    assert " · **1 no coverage**" in markdown
    assert "### No coverage (1)" in markdown
    assert "- `f.gd:3:11` · arithmetic · `+ -> -`" in markdown
    assert markdown.index("### No coverage") < markdown.index("### Surviving mutants")


def test_the_job_summary_without_survivors_still_lists_no_coverage() -> None:
    run = MutationRun(
        (
            MutantOutcome(_KILLED, Verdict.KILLED),
            MutantOutcome(_UNCOVERED, Verdict.NO_COVERAGE),
        ),
        coverage_analysis=True,
    )
    markdown = job_summary_markdown(run)
    assert "### No coverage (1)" in markdown
    assert "No surviving mutants." in markdown


def test_the_job_summary_is_unchanged_with_coverage_analysis_off() -> None:
    markdown = job_summary_markdown(MutationRun((MutantOutcome(_KILLED, Verdict.KILLED),)))
    assert "no coverage" not in markdown


def test_the_all_survived_warning_counts_no_coverage_as_undetected() -> None:
    run = MutationRun(
        (
            MutantOutcome(_SURVIVED, Verdict.SURVIVED),
            MutantOutcome(
                Mutant("g.gd", _UNCOVERED.span, "arithmetic", "+", "-"), Verdict.NO_COVERAGE
            ),
        ),
        coverage_analysis=True,
    )
    warning = all_survived_warning(run)
    assert warning is not None
    assert "all 2 evaluated mutants survived" in warning
    assert "f.gd, g.gd" in warning


def test_the_all_survived_warning_fires_on_two_no_coverage_mutants_alone() -> None:
    run = MutationRun(
        (
            MutantOutcome(_UNCOVERED, Verdict.NO_COVERAGE),
            MutantOutcome(_SURVIVED, Verdict.NO_COVERAGE),
        )
    )
    warning = all_survived_warning(run)
    assert warning is not None and "all 2 evaluated" in warning


def test_one_no_coverage_mutant_alone_stays_quiet() -> None:
    assert all_survived_warning(MutationRun((MutantOutcome(_UNCOVERED, Verdict.NO_COVERAGE),))) is (
        None
    )


# --- the CLI --------------------------------------------------------------------------------------

_GD = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"


def _gd(tmp_path: Path, name: str = "f.gd") -> Path:
    path = tmp_path / name
    path.write_text(_GD, encoding="utf-8")
    return path


def _capture(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake(*args: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, name, fake)
    return captured


@pytest.mark.parametrize("runner", ["gdunit4", "gut", "command"])
def test_the_option_reaches_the_single_file_run_with_a_marker_for_every_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    captured = _capture(monkeypatch, "run_mutation")
    argv = ["run", str(_gd(tmp_path)), "--project", str(tmp_path), "--runner", runner]
    argv += ["--coverage-analysis", "all", "--godot", "/g/godot"]
    if runner == "command":
        argv += ["--command", "h --path ."]
    assert main(argv) == 0
    assert captured["coverage"] is CoverageAnalysis.ALL
    assert captured["marker"] == GDScriptMarker(godot="/g/godot")


def test_the_option_reaches_the_many_file_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture(monkeypatch, "run_mutation_paths")
    first, second = _gd(tmp_path), _gd(tmp_path, "g.gd")
    argv = ["run", str(first), str(second), "--project", str(tmp_path), "--runner", "gut"]
    assert main([*argv, "--coverage-analysis", "all"]) == 0
    assert captured["coverage"] is CoverageAnalysis.ALL
    assert captured["marker"] == GDScriptMarker(godot="godot")


def test_the_option_defaults_to_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch, "run_mutation")
    assert main(["run", str(_gd(tmp_path)), "--project", str(tmp_path), "--runner", "gut"]) == 0
    assert captured["coverage"] is CoverageAnalysis.OFF


def test_per_file_reaches_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch, "run_mutation")
    argv = ["run", str(_gd(tmp_path)), "--project", str(tmp_path), "--runner", "gut"]
    assert main([*argv, "--coverage-analysis", "per-file"]) == 0
    assert captured["coverage"] is CoverageAnalysis.PER_FILE


def test_per_file_is_refused_for_the_command_runner_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exit code cannot say which tests ran, so a custom --command cannot be selected for.

    Refused rather than quietly downgraded to whole-suite runs, which would look exactly like
    selection working and take exactly as long as no selection at all."""
    captured = _capture(monkeypatch, "run_mutation")
    argv = ["run", str(_gd(tmp_path)), "--project", str(tmp_path), "--runner", "command"]
    argv += ["--command", "true"]
    assert main([*argv, "--coverage-analysis", "per-file"]) == 2
    assert captured == {}
    assert "error: --coverage-analysis per-file needs --runner gdunit4 or gut" in (
        capsys.readouterr().err
    )


def test_an_unknown_value_is_an_argparse_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["run", str(_gd(tmp_path)), "--runner", "gut", "--coverage-analysis", "some"])
    assert caught.value.code == 2


def test_dry_run_names_the_option_as_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(_gd(tmp_path)), "--dry-run", "--coverage-analysis", "all"]) == 0
    assert "--coverage-analysis is ignored" in capsys.readouterr().err


def test_dry_run_is_quiet_about_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(_gd(tmp_path)), "--dry-run"]) == 0
    assert "--coverage-analysis" not in capsys.readouterr().err


class _Failing:
    """A runner whose baseline passes, then a marker that cannot mark anything."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        return SuiteResult(1, 0, 0)

    def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        raise AssertionError("never reached")


class _Refuses:
    def mark(self, copy_dir: str, files: object) -> object:
        raise RuntimeError("the name is taken")


@pytest.mark.parametrize("many", [False, True])
def test_a_coverage_failure_exits_1_with_its_message_on_both_run_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], many: bool
) -> None:
    first = _gd(tmp_path)
    kwargs: dict[str, Any] = {"coverage": CoverageAnalysis.ALL, "marker": _Refuses()}
    if many:
        rc = cli.run_mutation_paths(
            [str(first), str(_gd(tmp_path, "g.gd"))], str(tmp_path), _Failing(), **kwargs
        )
    else:
        rc = cli.run_mutation(str(first), str(tmp_path), _Failing(), **kwargs)
    assert rc == 1
    err = capsys.readouterr().err
    assert (
        "error: could not prepare the marked copy for coverage analysis: the name is taken" in err
    )
    assert first.read_text(encoding="utf-8") == _GD


class _Lab:
    """A marker and runner that make every mutant on line 2 "no coverage"."""

    def __init__(self) -> None:
        self.hits_path: Path | None = None

    def mark(self, copy_dir: str, files: dict[str, tuple[str, list[Mutant]]]) -> Any:
        from gdmutant.engine.coverage import MarkedCopy

        self.hits_path = Path(copy_dir) / "hits.json"
        placements = {
            rel: tuple(0 if m.span.line == 2 else 9 for m in ms) for rel, (_, ms) in files.items()
        }
        return MarkedCopy(str(self.hits_path), placements)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        return SuiteResult(1, 0, 0)

    def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        assert self.hits_path is not None
        self.hits_path.write_text('{"hits": [9]}', encoding="utf-8")
        return SuiteResult(1, 0, 0)


def test_the_many_file_run_scores_and_summarises_no_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = _Lab()
    json_path = tmp_path / "r.json"
    rc = cli.run_mutation_paths(
        [str(_gd(tmp_path)), str(_gd(tmp_path, "g.gd"))],
        str(tmp_path),
        lab,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        json_path=str(json_path),
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Every mutant is on line 2, so every one is "no coverage": 0 detected out of all of them.
    assert "f.gd: 0.0%  (0 detected / 3)" in out
    assert "  no coverage: 6  " in out
    assert "Coverage self-check: re-ran 3 of the 6 no-coverage mutants" in out
    report = json.loads(json_path.read_text(encoding="utf-8"))
    statuses = {m["status"] for f in report["files"].values() for m in f["mutants"]}
    assert statuses == {"NoCoverage"}


def test_the_single_file_run_summarises_no_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab = _Lab()
    rc = cli.run_mutation(
        str(_gd(tmp_path)), str(tmp_path), lab, coverage=CoverageAnalysis.ALL, marker=lab
    )
    assert rc == 0
    assert "Coverage self-check: re-ran 3 of the 3 no-coverage mutants" in capsys.readouterr().out


def test_the_many_file_aggregate_says_the_self_check_ran_even_with_nothing_uncovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Reached(_Lab):
        def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            assert self.hits_path is not None
            self.hits_path.write_text('{"hits": [0]}', encoding="utf-8")
            return SuiteResult(1, 0, 0)

    lab = Reached()
    cli.run_mutation_paths(
        [str(_gd(tmp_path)), str(_gd(tmp_path, "g.gd"))],
        str(tmp_path),
        lab,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
    )
    assert "Coverage self-check: compared 0 mutants" in capsys.readouterr().out


def test_the_failure_class_is_what_the_cli_catches() -> None:
    from gdmutant.engine.loop import BaselineFailed

    assert issubclass(CoverageRunFailed, BaselineFailed)


def test_the_console_block_and_the_markdown_block_are_laid_out_exactly() -> None:
    text = console_summary(_run())
    assert (
        "No coverage (1): no test reaches these lines, so start with a test that runs them.\n\n"
        "  f.gd:3:11  arithmetic  + -> -\n\nResults"
    ) in text
    markdown = job_summary_markdown(_run())
    assert (
        "\n\n### No coverage (1)\n\nNo test reaches these lines, so start with a test that runs "
        "them:\n\n- `f.gd:3:11` \u00b7 arithmetic \u00b7 `+ -> -`\n\n### Surviving"
    ) in markdown
    assert (
        "1 killed \u00b7 0 timeout \u00b7 **1 survived** \u00b7 0 ignored \u00b7 0 invalid \u00b7 "
        "0 error \u00b7 **1 no coverage**\n"
    ) in markdown


def test_the_tally_line_ends_at_error_with_coverage_off() -> None:
    markdown = job_summary_markdown(MutationRun((MutantOutcome(_KILLED, Verdict.KILLED),)))
    assert "\u00b7 0 invalid \u00b7 0 error\n" in markdown


def test_the_no_coverage_narrative_says_what_it_is() -> None:
    assert NO_COVERAGE_GAP.startswith("No test reaches this line")
    assert NO_COVERAGE_REASON.startswith("Nothing runs this code during the test suite")
    assert NO_COVERAGE_REASON.endswith("whether that test also checks what the line does.")


def test_a_file_with_no_no_coverage_mutants_scores_on_its_own_counts() -> None:
    report = stryker_report(
        MutationRun(
            (MutantOutcome(_KILLED, Verdict.KILLED), MutantOutcome(_SURVIVED, Verdict.SURVIVED))
        ),
        "f.gd",
        _SRC,
        "gdscript",
    )
    (file_view,) = report_view(report).files
    assert (file_view.no_coverage, file_view.score) == (0, 50.0)
