"""Tests for the reporter (console summary + Stryker JSON)."""

import json

from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import _kill_hint, console_summary, html_report, stryker_report
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


def test_ignored_maps_to_stryker_ignored_with_reason_and_excluded_from_score() -> None:
    # An ignored mutant renders as Stryker "Ignored" and carries its reason as statusReason (omitted
    # when the reason is empty); it's excluded from the score like invalid/error.
    run = MutationRun(
        (
            MutantOutcome(
                Mutant(
                    "f.gd", Span(2, 9, 2, 10), "comparison", ">", ">=", ignore_reason="equivalent"
                ),
                Verdict.IGNORED,
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 13, 2, 14), "numeric", "0", "1", ignore_reason=""),
                Verdict.IGNORED,
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 21, 2, 22), "boolean", "and", "or"), Verdict.SURVIVED
            ),
        )
    )
    mutants = stryker_report(run, "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"]
    assert mutants[0]["status"] == "Ignored" and mutants[0]["statusReason"] == "equivalent"
    assert mutants[1]["status"] == "Ignored" and "statusReason" not in mutants[1]  # empty → no key
    assert run.mutation_score == 0.0  # 0 detected / (0 + 1 survived); the 2 ignored are excluded
    assert "ignored:  2  (suppressed, excluded from score)" in console_summary(run)


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
        "  ignored:  0  (suppressed, excluded from score)\n"
        "  invalid:  1\n"
        "  error:    1\n"
        "\n"
        "Survivors (1):\n"
        "  f.gd:2:15  boolean  and -> or\n"
        "      → kill it: test operands that disagree (one true, one false) so and vs or matters"
    )


def test_console_summary_renders_a_deletion_survivor_as_deleted() -> None:
    # A deletion operator (unary-`not` removal) has an empty replacement. It must not render as a
    # dangling "not -> " ([ticket]) — the survivors list should say "(deleted)".
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 8, 2, 11), "logical-not", "not", ""), Verdict.SURVIVED
            ),
        )
    )
    out = console_summary(run)
    assert "f.gd:2:8  logical-not  not -> (deleted)" in out
    assert "not -> \n" not in out and not out.endswith("not -> ")


def test_console_summary_appends_a_kill_hint_per_survivor() -> None:
    # Each survivor gets a "→ kill it:" hint line so the list reads as actionable, not homework
    # ([ticket]). One survivor here (the boolean and -> or) → one hint.
    out = console_summary(_run())
    assert out.count("→ kill it:") == 1
    assert _kill_hint("boolean") in out


def test_kill_hint_is_operator_specific_with_a_generic_fallback() -> None:
    assert "boundary" in _kill_hint("comparison")  # operator-specific
    assert _kill_hint("some-custom-operator") == "write a test that fails under this exact change"


def test_reading_your_first_report_doc_sample_matches_the_real_hint() -> None:
    # The human doc shows a sample survivor line with its hint; keep that sample honest so it can't
    # drift from _KILL_HINTS ([ticket] review). If you reword the comparison hint, update the doc.
    from pathlib import Path

    doc = Path(__file__).resolve().parent.parent / "docs" / "reading-your-first-report.md"
    assert _kill_hint("comparison") in doc.read_text(encoding="utf-8")


def test_console_summary_score_na_when_no_killable_mutants() -> None:
    run = MutationRun(
        (MutantOutcome(Mutant("f.gd", Span(1, 1, 1, 2), "x", "a", "b"), Verdict.INVALID),)
    )
    out = console_summary(run)
    assert "Mutation score: n/a" in out
    assert "Survivors" not in out  # none to list


def test_html_report_embeds_the_report_and_the_pinned_viewer() -> None:
    # --html writes a ready-to-open page: the mutation-testing-elements viewer (pinned CDN) with the
    # Stryker report inlined in a non-executable <script type="application/json"> block ([ticket]).
    report = stryker_report(_run(), "f.gd", _SRC, "gdscript")
    html = html_report(report)
    assert "<mutation-test-report-app>" in html
    assert "mutation-testing-elements@3.8.4" in html  # pinned version
    block = html.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block) == report  # the inlined JSON parses back to the exact report


def test_html_report_escapes_script_close_in_source_so_it_cannot_break_out() -> None:
    # GDScript source containing `</script>` must not close the data block early. It's escaped to
    # `<\/script>` (valid JSON), so it never appears raw in the data yet round-trips on parse.
    report = {
        "schemaVersion": "2",
        "files": {
            "x.gd": {"language": "gdscript", "source": 'var s := "</script>"', "mutants": []}
        },
    }
    html = html_report(report)
    block = html.split('id="mutation-test-report">', 1)[1].split("</script>", 1)[0]
    assert "</script>" not in block  # never raw inside the data block
    assert json.loads(block)["files"]["x.gd"]["source"] == 'var s := "</script>"'  # restored
