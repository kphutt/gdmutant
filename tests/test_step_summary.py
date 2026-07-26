"""Tests for the survivor job-summary reporter: `report.job_summary_markdown` (the Markdown) and
the CLI's `--report step-summary` wiring that writes it to `$GITHUB_STEP_SUMMARY` (the GitHub
Actions job summary) when set, and to stdout otherwise."""

from pathlib import Path

import pytest
from conftest import MarkerRunner

import gdmutant.cli as cli
from gdmutant.cli import _emit_step_summary, main, run_mutation, run_mutation_paths
from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import job_summary_markdown
from gdmutant.engine.spans import Span

_BOOLEAN_DOC = "https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md#boolean"


def _boolean_survivor(path: str = "f.gd") -> MutantOutcome:
    """A boolean `and`->`or` survivor at `path`:2 (the operator whose narrative the tests check)."""
    mutant = Mutant(path, Span(2, 15, 2, 18), "boolean", "and", "or")
    return MutantOutcome(mutant, Verdict.SURVIVED)


def _gd(tmp_path: Path) -> Path:
    """A tiny two-survivor GDScript file (`>`->`>=` kept by a `>=` marker; `and`/`<` survive)."""
    path = tmp_path / "f.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a > b and a < b\n", encoding="utf-8")
    return path


# --- job_summary_markdown: the Markdown itself --------------------------------


def test_job_summary_markdown_renders_score_tally_and_the_survivor_explanation() -> None:
    # The differentiator: the summary carries each survivor's gap/risk/start EXPLANATION (reused
    # from survivor_report_fields — the same copy the console block and Stryker JSON carry), not
    # just a location. Plus the score, the per-verdict tally, and the stable per-operator docs link.
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED
            ),
            _boolean_survivor(),
        )
    )
    md = job_summary_markdown(run)
    assert md.startswith("## gdmutant — mutation report")
    assert "**Mutation score: 50.0%**" in md
    assert "1 killed · 0 timeout · **1 survived** · 0 ignored · 0 invalid · 0 error" in md
    assert "### Surviving mutants (1)" in md
    assert "#### `f.gd:2` · boolean" in md
    assert "**The gap.** Your tests pass whether this needs both sides" in md
    assert "**Why it matters, and where to start.**" in md
    assert "Add a test where exactly one side is true and the other false" in md
    assert f"[Explain the `boolean` operator]({_BOOLEAN_DOC})" in md
    assert md.endswith("\n")  # trailing newline so it appends cleanly to the summary file


def test_job_summary_markdown_includes_the_source_line_and_change_note_when_readable(
    tmp_path: Path,
) -> None:
    # A readable source file yields a fenced code block (tabs expanded to 4) plus a plain note of
    # the change — the Markdown peer of the console caret.
    path = tmp_path / "m.gd"
    path.write_text("func f(a, b):\n\treturn a and b\n", encoding="utf-8")
    m = Mutant(str(path), Span(2, 11, 2, 14), "boolean", "and", "or")
    md = job_summary_markdown(MutationRun((MutantOutcome(m, Verdict.SURVIVED),)))
    assert "```gdscript" in md
    assert "    return a and b" in md  # the tab was expanded to four spaces
    assert "Changed `and` to `or` — every test still passed." in md


def test_job_summary_markdown_shows_a_line_one_survivor_source(tmp_path: Path) -> None:
    # A survivor on line 1 still renders its source (pins the lower bound of the readable-line
    # guard: a `1 <= line_no` -> `1 < line_no` mutant would drop the code block for line 1).
    path = tmp_path / "m.gd"
    path.write_text("var ready := true\n", encoding="utf-8")
    m = Mutant(str(path), Span(1, 13, 1, 17), "constant", "true", "false")
    md = job_summary_markdown(MutationRun((MutantOutcome(m, Verdict.SURVIVED),)))
    assert "```gdscript" in md
    assert "var ready := true" in md


def test_job_summary_markdown_drops_the_code_block_when_source_is_unreadable() -> None:
    # With no readable source (path not on disk) the code block and change note drop out gracefully,
    # and the explanation still stands.
    md = job_summary_markdown(MutationRun((_boolean_survivor(path="no_such_dir/f.gd"),)))
    assert "```gdscript" not in md
    assert "every test still passed" not in md  # the change note rides with the (absent) code block
    assert "**The gap.**" in md  # the narrative is always present


def test_job_summary_markdown_renders_a_deletion_change_note(tmp_path: Path) -> None:
    # A statement-deletion survivor is phrased as a whole-line removal, not a dangling `x -> `.
    path = tmp_path / "m.gd"
    path.write_text("func f():\n\tprint(x)\n", encoding="utf-8")
    m = Mutant(str(path), Span(2, 2, 2, 10), "statement-deletion", "print(x)", "")
    md = job_summary_markdown(MutationRun((MutantOutcome(m, Verdict.SURVIVED),)))
    assert "This whole line was removed — every test still passed." in md


def test_job_summary_markdown_renders_a_removal_change_note_for_an_empty_replacement(
    tmp_path: Path,
) -> None:
    # A dropped-token survivor (a `not` removed, empty replacement) reads as a removal, not
    # a dangling `not -> `.
    path = tmp_path / "m.gd"
    path.write_text("func f(a):\n\treturn not a\n", encoding="utf-8")
    m = Mutant(str(path), Span(2, 9, 2, 12), "logical-not", "not", "")
    md = job_summary_markdown(MutationRun((MutantOutcome(m, Verdict.SURVIVED),)))
    assert "Removed `not` — every test still passed." in md


def test_job_summary_markdown_says_all_caught_when_there_are_no_survivors() -> None:
    run = MutationRun(
        (MutantOutcome(Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED),)
    )
    md = job_summary_markdown(run)
    assert "**Mutation score: 100.0%**" in md
    assert "No surviving mutants" in md
    assert "### Surviving mutants" not in md  # no survivor section when there are none


def test_job_summary_markdown_score_is_na_when_no_killable_mutants() -> None:
    run = MutationRun(
        (MutantOutcome(Mutant("f.gd", Span(1, 1, 1, 2), "x", "a", "b"), Verdict.INVALID),)
    )
    assert "**Mutation score: n/a**" in job_summary_markdown(run)


# --- _emit_step_summary: where the Markdown goes ------------------------------


def test_emit_step_summary_writes_markdown_to_the_github_step_summary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The falsifiable check ([ticket]): with $GITHUB_STEP_SUMMARY set, the reporter writes the
    # survivor Markdown to that file. Fails today — no such reporter existed.
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _emit_step_summary(MutationRun((_boolean_survivor(),)))
    written = summary.read_text(encoding="utf-8")
    assert "## gdmutant — mutation report" in written
    assert "**The gap.**" in written  # the explanation, not just a list


def test_emit_step_summary_appends_rather_than_truncating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub accumulates the job summary across steps, so the reporter must APPEND — an earlier
    # step's content must survive.
    summary = tmp_path / "summary.md"
    summary.write_text("PRIOR STEP\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _emit_step_summary(MutationRun((_boolean_survivor(),)))
    written = summary.read_text(encoding="utf-8")
    assert written.startswith("PRIOR STEP\n")  # the earlier step's content is preserved
    assert "## gdmutant — mutation report" in written


def test_emit_step_summary_prints_to_stdout_when_the_env_var_is_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No $GITHUB_STEP_SUMMARY (running locally) -> the Markdown goes to stdout instead.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _emit_step_summary(MutationRun((_boolean_survivor(),)))
    assert "## gdmutant — mutation report" in capsys.readouterr().out


def test_emit_step_summary_warns_and_continues_on_a_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The reporter is advisory: an unwritable summary path (its parent dir doesn't exist) warns on
    # stderr rather than crashing the run.
    bad = tmp_path / "no_such_dir" / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(bad))
    _emit_step_summary(MutationRun((_boolean_survivor(),)))
    assert "could not write the job summary" in capsys.readouterr().err


# --- CLI wiring: run_mutation / run_mutation_paths / main ---------------------


def test_run_mutation_step_summary_writes_survivors_to_the_summary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gd(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="), step_summary=True)
    assert rc == 0
    written = summary.read_text(encoding="utf-8")
    assert "## gdmutant — mutation report" in written
    assert "Surviving mutants" in written


def test_run_mutation_writes_nothing_to_the_summary_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # step_summary defaults False: with the env var set but the flag off, the summary is untouched.
    path = _gd(tmp_path)
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run_mutation(str(path), str(tmp_path), MarkerRunner(str(path), ">="))
    assert summary.read_text(encoding="utf-8") == ""  # untouched


def test_run_mutation_paths_step_summary_writes_aggregate_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The multi-file path emits ONE aggregate summary listing survivors across every file.
    a = tmp_path / "a.gd"
    a.write_text("func f(x, y) -> bool:\n\treturn x > y\n", encoding="utf-8")
    b = tmp_path / "b.gd"
    b.write_text("func g(x, y) -> bool:\n\treturn x < y\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    # A marker never present in the source -> every mutant across both files survives.
    rc = run_mutation_paths(
        [str(a), str(b)], str(tmp_path), MarkerRunner(str(a), "ZZZ"), step_summary=True
    )
    assert rc == 0
    written = summary.read_text(encoding="utf-8")
    assert "Surviving mutants" in written
    assert "a.gd" in written and "b.gd" in written  # both files' survivors in one summary


def test_main_report_step_summary_writes_the_job_summary_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--report step-summary` threads step_summary=True all the way through main().
    path = _gd(tmp_path)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(str(path), ">="))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = main(["run", str(path), "--project", str(tmp_path), "--report", "step-summary"])
    assert rc == 0
    written = summary.read_text(encoding="utf-8")
    assert "## gdmutant — mutation report" in written
    assert "Surviving mutants" in written


def test_dry_run_notes_that_report_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run runs no tests, so there's nothing to summarize; --report is flagged as ignored.
    path = _gd(tmp_path)
    rc = main(["run", str(path), "--report", "step-summary", "--dry-run"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "--report" in err and "ignored" in err
