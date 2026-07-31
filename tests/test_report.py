"""Tests for the reporter (console summary + Stryker JSON)."""

import json
import re
from pathlib import Path

from gdmutant.engine.explain import render_survivor
from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import (
    all_survived_warning,
    console_summary,
    html_report,
    stryker_report,
)
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


def test_report_json_survivor_carries_the_gap_risk_start_narrative() -> None:
    # A survivor's report JSON now carries the same explanation the console block shows: the gap in
    # `description`, the risk + starting point in `statusReason` — so the HTML viewer explains *why
    # it matters* and *where to start*, not just the diff (was empty for survivors before).
    survivor = stryker_report(_run(), "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"][1]
    assert survivor["status"] == "Survived"
    # description = the gap sentence (what the tests miss), for this boolean `and`->`or` survivor
    assert survivor["description"] == (
        "Your tests pass whether this needs both sides (`and`) or just one (`or`). No test covers "
        "the case that tells them apart: the operands disagreeing (one true, one false)."
    )
    # statusReason = risk (why it matters) + start (where to begin), blank-line separated
    assert survivor["statusReason"] == (
        "Your tests can't tell 'needs both' from 'needs either.' A change that loosens or tightens "
        "this guard would pass every test."
        "\n\n"
        "Add a test where exactly one side is true and the other false, and assert the outcome."
    )


def test_report_json_only_survivors_get_the_narrative() -> None:
    # The narrative fields are survivor-only: killed/invalid/error mutants carry no `description`
    # and no survivor `statusReason`; the ignored mutant keeps its own ignore-reason statusReason
    # and gains no `description` — so populating survivors doesn't disturb any other verdict.
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 13, 2, 14), "numeric", "0", "1", ignore_reason="equivalent"),
                Verdict.IGNORED,
            ),
            MutantOutcome(
                Mutant("f.gd", Span(2, 15, 2, 18), "boolean", "and", "or"), Verdict.SURVIVED
            ),
        )
    )
    killed, ignored, survived = stryker_report(run, "f.gd", _SRC, "gdscript")["files"]["f.gd"][
        "mutants"
    ]
    assert "description" not in killed and "statusReason" not in killed
    assert "description" not in ignored and ignored["statusReason"] == "equivalent"  # unchanged
    assert survived["description"] and survived["statusReason"]  # both populated for the survivor


def test_report_json_survivor_narrative_omits_console_only_decoration() -> None:
    # The report fields are the trimmed narrative: the three sentences, none of the console block's
    # box-drawing, caret, or the `more` docs link the HTML viewer draws for itself.
    survivor = stryker_report(_run(), "f.gd", _SRC, "gdscript")["files"]["f.gd"]["mutants"][1]
    blob = survivor["description"] + survivor["statusReason"]
    assert "─" not in blob and "^" not in blob and "docs/survivors" not in blob


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
    # New per-survivor explanation block: header with category, then gap/risk/start/more.
    assert "survived" in out and "boolean ─" in out
    assert "f.gd:2" in out
    assert "gap    Your tests pass whether this needs both sides" in out


def test_console_summary_survivor_block_has_all_slots_and_the_doc_link() -> None:
    # Each survivor renders the locked 7-slot block. With no readable source file (f.gd is not on
    # disk), the code/caret/enclosing-func slots drop out gracefully and the narrative still stands.
    out = console_summary(_run())
    for label in ("  gap    ", "  risk   ", "  start  ", "  more   "):
        assert label in out, f"missing slot {label!r}"
    # the `more` link is the stable per-operator docs anchor (ShellCheck model)
    assert "docs/survivors/README.md#boolean" in out
    # f.gd is not readable here, so there is no code line or caret
    assert "2 | " not in out and "^" not in out


def test_readme_shows_the_current_console_format_not_the_retired_one() -> None:
    # The old "→ kill it" one-liner format is retired; no shipping doc may still describe it, and
    # the README's sample survivor must match what render_survivor produces — reinstates the
    # doc-sync guard removed with _kill_hint (flagged in review of the slice-1 PR). Retargeted from
    # the now-deleted docs/reading-your-first-report.md to README.md, the annotated survivor's one
    # remaining shipping home (its content moved into README's example output +
    # docs/survivors/README.md's intro).
    repo = Path(__file__).resolve().parent.parent
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert "→ kill it" not in readme
    assert "──── survived" in readme  # shows the new block
    # the README's example survivor (turn_order.gd:13, `<` -> `<=`) must match the renderer's real
    # output
    src = (repo / "corpus" / "turn_order.gd").read_text(encoding="utf-8").splitlines()
    m = Mutant("turn_order.gd", Span(13, 11, 13, 12), "comparison", "<", "<=")
    caret = next(line for line in render_survivor(m, src) if "changed  <  to  <=" in line)
    assert caret in readme


def test_console_summary_wraps_each_survivor_block_exactly() -> None:
    # Pin console_summary's own wrapper (the blank separators around "Survivors" and the newline
    # join) around the render_survivor block. f.gd is not on disk, so the block renders source-less.
    m = Mutant("no_such_dir/f.gd", Span(2, 15, 2, 18), "boolean", "and", "or")
    run = MutationRun((MutantOutcome(m, Verdict.SURVIVED),))
    block = "\n".join(render_survivor(m, None))
    assert console_summary(run).endswith("\n\nSurvivors (1):\n\n" + block + "\n")


def test_console_summary_reads_the_real_source_for_the_code_and_caret(tmp_path: Path) -> None:
    # When the file is readable, the block shows the real source line + enclosing func — proving the
    # summary actually reads the source (not a hard-coded None).
    path = tmp_path / "m.gd"
    path.write_text("func f():\n\treturn a and b\n", encoding="utf-8")
    m = Mutant(str(path), Span(2, 11, 2, 14), "boolean", "and", "or")
    out = console_summary(MutationRun((MutantOutcome(m, Verdict.SURVIVED),)))
    assert "return a and b" in out  # the real source line (source was read, not None)
    assert "func f" in out  # enclosing function pulled from the real file


def test_console_summary_start_never_suggests_an_assertion_value() -> None:
    # The safety invariant: `start` names the missing INPUT, never the expected/oracle value —
    # suggesting one could codify a bug. It must say the answer is the developer's.
    out = console_summary(
        MutationRun(
            (
                MutantOutcome(
                    Mutant("f.gd", Span(2, 8, 2, 9), "comparison", ">", ">="), Verdict.SURVIVED
                ),
            )
        )
    )
    assert "reports the gap, not it" in out  # explicitly declines to assert the answer


def test_console_summary_renders_a_deletion_survivor_without_a_dangling_arrow() -> None:
    # A deletion operator (unary-`not` removal) has an empty replacement — it must not render as a
    # dangling "-> ". The statement-deletion narrative covers the whole-line removal.
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 8, 2, 11), "statement-deletion", "print(x)", ""),
                Verdict.SURVIVED,
            ),
        )
    )
    out = console_summary(run)
    assert "statement-deletion ─" in out
    assert "gap    Your tests pass with this line removed entirely" in out
    assert "-> \n" not in out and "-> (deleted)" not in out


def test_console_summary_score_na_when_no_killable_mutants() -> None:
    run = MutationRun(
        (MutantOutcome(Mutant("f.gd", Span(1, 1, 1, 2), "x", "a", "b"), Verdict.INVALID),)
    )
    out = console_summary(run)
    assert "Mutation score: n/a" in out
    assert "Survivors" not in out  # none to list


def _survivor(path: str, col: int) -> MutantOutcome:
    mutant = Mutant(path, Span(2, col, 2, col + 1), "boolean", "and", "or")
    return MutantOutcome(mutant, Verdict.SURVIVED)


def test_all_survived_warning_fires_when_baseline_passed_and_every_mutant_survived() -> None:
    # The vacuous "all survived" fingerprint: zero detected, several survivors. The warning names
    # the mutated file and points at the likely fix — the tests never touched it.
    run = MutationRun((_survivor("game/board.gd", 9), _survivor("game/board.gd", 15)))
    warning = all_survived_warning(run)
    assert warning is not None
    assert "all 2 evaluated mutants survived" in warning
    assert "game/board.gd" in warning  # names the mutated file
    assert "--tests" in warning  # points at the fix


def test_all_survived_warning_silent_when_a_mutant_was_detected() -> None:
    # One kill means a test does reach this file — not the vacuous case, so no warning (option A
    # only catches a *total* miss; a partial miss is a weak suite reported honestly).
    run = MutationRun(
        (
            MutantOutcome(
                Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">="), Verdict.KILLED
            ),
            _survivor("f.gd", 15),
            _survivor("f.gd", 21),
        )
    )
    assert all_survived_warning(run) is None


def test_all_survived_warning_silent_on_a_single_surviving_mutant() -> None:
    # Don't cry wolf on a 1-of-1 survivor: too small a sample to tell "tests miss this file" apart
    # from one genuinely-surviving mutant.
    run = MutationRun((_survivor("f.gd", 9),))
    assert all_survived_warning(run) is None


def test_all_survived_warning_ignores_unscored_verdicts() -> None:
    # invalid/error/ignored don't count toward the score, so a run that is all-survived among its
    # scored mutants still warns even with unscored mutants mixed in.
    run = MutationRun(
        (
            _survivor("f.gd", 9),
            _survivor("f.gd", 15),
            MutantOutcome(Mutant("f.gd", Span(3, 1, 3, 2), "x", "a", "b"), Verdict.INVALID),
            MutantOutcome(Mutant("f.gd", Span(4, 1, 4, 2), "x", "a", "b"), Verdict.ERROR),
        )
    )
    assert all_survived_warning(run) is not None


def test_html_report_is_one_self_contained_file_carrying_the_report() -> None:
    # --html writes a ready-to-open page that needs NOTHING else: no CDN, no fonts, no images. The
    # Stryker report rides in a non-executable <script type="application/json"> block for tooling.
    # (The page itself is covered in depth by tests/test_htmlreport.py.)
    report = stryker_report(_run(), "f.gd", _SRC, "gdscript")
    html = html_report(report)
    assert "<html" in html
    assert "unpkg" not in html
    # Every <link> the page carries must be inlined (the favicon `data:` URI), never a fetch.
    assert [tag for tag in re.findall(r"<link\b[^>]*>", html) if 'href="data:' not in tag] == []
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
