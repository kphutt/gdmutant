"""Tests for the reporter (console summary + Stryker JSON)."""

from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import console_summary, stryker_report
from gdmutant.engine.spans import Span

_SRC = "func f(a, b):\n\treturn a > b and a < b\n\treturn a + b\n"


def _run() -> MutationRun:
    return MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 15, 2, 18), "boolean", "and", "or"), Verdict.SURVIVED
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 21, 2, 22), "comparison", "<", "<="), Verdict.INVALID
            ),
            MutantOutcome(
                Mutant("f.gd", Span(3, 10, 3, 11), "arithmetic", "+", "-"), Verdict.ERROR
            ),
        )
    )


def test_stryker_report_top_level_and_file_shape() -> None:
    report = stryker_report(_run(), "f.gd", _SRC, "gdscript")
    assert report["schemaVersion"] == "2"
    assert report["thresholds"] == {"high": 80, "low": 60}
    file = report["files"]["f.gd"]
    assert file["language"] == "gdscript"
    assert file["source"] == _SRC
    assert len(file["mutants"]) == 4


def test_stryker_report_status_mapping() -> None:
    mutants = stryker_report(_run(), "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"]
    assert [m["status"] for m in mutants] == ["Killed", "Survived", "CompileError", "RuntimeError"]


def test_timeout_maps_to_stryker_timeout_and_counts_as_detected() -> None:
    # A timeout is a detection: it renders as Stryker "Timeout" and is counted toward the score like
    # a kill (Stryker convention), not excluded like invalid/error.
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED
            ),
            MutantOutcome(Mutant("f.gd", Span(2, 15, 2, 16), "numeric", "1", "0"), Verdict.TIMEOUT),
            MutantOutcome(
                Mutant("f.gd", Span(2, 21, 2, 22), "boolean", "and", "or"), Verdict.SURVIVED
            ),
        )
    )
    statuses = [
        m["status"]
        for m in stryker_report(run, "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"]
    ]
    assert statuses == ["Killed", "Timeout", "Survived"]
    # detected = killed(1) + timeout(1) = 2; score = 2 / (2 + 1 survived)
    assert run.mutation_score == 2 / 3
    assert "timeout:  1  (counted as killed)" in console_summary(run)


def test_stryker_report_mutant_fields_and_location() -> None:
    first = stryker_report(_run(), "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"][0]
    assert first["id"] == "0"
    assert first["mutatorName"] == "comparison"
    assert first["replacement"] == ">="
    assert first["location"] == {
        "start": {"line": 2, "column": 9},
        "end": {"line": 2, "column": 10},
    }


def test_stryker_report_ids_are_unique() -> None:
    mutants = stryker_report(_run(), "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"]
    assert len({m["id"] for m in mutants}) == len(mutants)


def test_stryker_report_carries_the_given_language() -> None:
    report = stryker_report(_run(), "x.py", "pass\n", language="python")
    assert report["files"]["x.py"]["language"] == "python"


def test_console_summary_score_counts_and_survivors() -> None:
    out = console_summary(_run())  # killed=1, survived=1 -> 50.0%
    assert "Mutation score: 50.0%" in out
    assert "killed:   1" in out and "survived: 1" in out
    assert "invalid:  1" in out and "error:    1" in out
    assert "Survivors (1):" in out
    assert "f.gd:2:15  boolean  and -> or" in out


def test_console_summary_exact_layout() -> None:
    # Pin the whole rendered block: the blank separator line before "Survivors" and the "\n" join
    # (mutants that turn "" into junk or join on a different separator are caught).
    assert console_summary(_run()) == (
        "Mutation score: 50.0%\n"
        "  killed:   1\n"
        "  timeout:  0  (counted as killed)\n"
        "  survived: 1\n"
        "  invalid:  1\n"
        "  error:    1\n"
        "\n"
        "Survivors (1):\n"
        "  f.gd:2:15  boolean  and -> or"
    )


def test_console_summary_score_na_when_no_killable_mutants() -> None:
    run = MutationRun(
        (MutantOutcome(Mutant("f.gd", Span(1, 1, 1, 2), "x", "a", "b"), Verdict.INVALID),)
    )
    out = console_summary(run)
    assert "Mutation score: n/a" in out
    assert "Survivors" not in out  # none to list
