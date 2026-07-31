"""Reporting: a console summary + the Stryker mutation-testing-elements JSON report (FG-5).

The JSON follows the mutation-testing-elements report schema (v2), so it renders in that
ecosystem's HTML viewer. Verdicts map to the schema's ``MutantStatus``:

    killed -> Killed, survived -> Survived, timeout -> Timeout, ignored -> Ignored,
    invalid -> CompileError, error -> RuntimeError.

A suppressed (ignored) mutant carries its ``# gdmutant: ignore`` reason as the schema's optional
``statusReason`` field, so the viewer shows *why* it was ignored. A **survivor** carries the same
gap/risk/start narrative the console block shows, split across the schema's ``description`` (the
gap) and ``statusReason`` (the risk + where to start), so the HTML viewer explains it too.

Locations are 1-based line + column with an exclusive end column, which is exactly what a
`Span` carries, so no coordinate conversion is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gdmutant.engine.explain import (
    ASSERT_SECTION,
    doc_url,
    reference_section,
    render_survivor,
    source_line,
    survivor_report_fields,
)
from gdmutant.engine.htmlreport import change_note, render_html
from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict
from gdmutant.engine.mutants import Mutant

SCHEMA_VERSION = "2"

_STATUS: dict[Verdict, str] = {
    Verdict.KILLED: "Killed",
    Verdict.SURVIVED: "Survived",
    Verdict.TIMEOUT: "Timeout",
    Verdict.IGNORED: "Ignored",
    Verdict.INVALID: "CompileError",
    Verdict.ERROR: "RuntimeError",
}


def _mutant_json(index: int, outcome: MutantOutcome, source_lines: list[str]) -> dict[str, Any]:
    m = outcome.mutant
    mutant: dict[str, Any] = {
        "id": str(index),
        "mutatorName": m.operator_id,
        "replacement": m.replacement,
        "location": {
            "start": {"line": m.span.line, "column": m.span.column},
            "end": {"line": m.span.end_line, "column": m.span.end_column},
        },
        "status": _STATUS[outcome.verdict],
    }
    # An ignored mutant's reason (if the annotation gave one) rides along as the schema's optional
    # statusReason — omitted when empty, so a bare `# gdmutant: ignore` adds no key.
    if outcome.verdict is Verdict.IGNORED and m.ignore_reason:
        mutant["statusReason"] = m.ignore_reason
    # A survivor carries the same gap/risk/start narrative the console block shows, so the HTML
    # viewer explains *why it matters* and *where to start* — not just the diff. `description` gets
    # the gap; `statusReason` gets the risk + starting point. (Killed/timeout/invalid/error mutants
    # need no such narrative; ignored keeps its own reason above.)
    if outcome.verdict is Verdict.SURVIVED:
        # The report carries the file's own source, so the narrative gets the mutated line for
        # free — which is what lets an assert survivor explain itself in the JSON and the HTML
        # report, not only on the console.
        src = source_line(m, source_lines)
        mutant["description"], mutant["statusReason"] = survivor_report_fields(m, src)
    return mutant


def _file_entry(run: MutationRun, source: str, language: str) -> dict[str, Any]:
    """One file's entry in the report's `files` map."""
    source_lines = source.split("\n")
    return {
        "language": language,
        "source": source,
        "mutants": [
            _mutant_json(index, outcome, source_lines) for index, outcome in enumerate(run.outcomes)
        ],
    }


def stryker_report_multi(
    files: dict[str, tuple[MutationRun, str]], language: str
) -> dict[str, Any]:
    """Build a merged mutation-testing-elements report for one or more files. `files` maps
    each file path to its ``(run, source)``. The schema keys `files` by path, so a whole-directory
    run renders as one report with per-file drill-down and one overall score in the viewer.

    `language` is supplied by the caller (the adapter/CLI knows it) — the reporter stays
    language-neutral and carries no default.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "thresholds": {"high": 80, "low": 60},
        "files": {
            path: _file_entry(run, source, language) for path, (run, source) in files.items()
        },
    }


def stryker_report(run: MutationRun, path: str, source: str, language: str) -> dict[str, Any]:
    """Build the mutation-testing-elements report dict for a single-file `run`."""
    return stryker_report_multi({path: (run, source)}, language)


def html_report(report: dict[str, Any]) -> str:
    """A ready-to-open HTML report — **one self-contained file**, rendered by `htmlreport`.

    Everything the page needs is inlined: styles, script, and the mascot. It opens with no network
    at all, which is what makes it usable as a CI artifact, an email attachment, or an offline read.
    The full Stryker `report` dict rides along in a non-executable
    ``<script type="application/json">`` block so the file stays machine-readable for other tooling.
    """
    return render_html(report)


#: Below this many surviving mutants the all-survived warning stays quiet: a 1-of-1 survivor is too
#: small a sample to tell "the tests never touched this file" apart from a single
#: genuinely-surviving mutant, so warning there would cry wolf.
_MIN_SURVIVORS_FOR_ALL_SURVIVED_WARNING = 2


def all_survived_warning(run: MutationRun) -> str | None:
    """A stderr warning for the "vacuous all-survived" case, or ``None`` when it doesn't apply.

    A ``MutationRun`` only exists once the baseline passed (`loop._run_baseline` raises otherwise),
    so a run whose every score-counting mutant survived — zero detected (``killed`` + ``timeout``),
    every scored mutant in the ``survived`` bucket — is the fingerprint of a test command that runs
    green but never actually exercises the mutated file. That reads as "your tests catch nothing"
    when the truth is "nothing was tested" — the tool's most dangerous false signal. Surface it as a
    warning (never an error: the score and exit code are unchanged) that names the mutated file and
    points at the likely fix.

    This is the cheap heuristic, not a coverage probe (DESIGN.md FG-4.1 folds no-coverage into
    survived for v0.1); it reads only the finalized tally. Stays quiet below
    `_MIN_SURVIVORS_FOR_ALL_SURVIVED_WARNING` survivors so a lone surviving mutant doesn't trip it.
    """
    if run.detected != 0 or run.survived < _MIN_SURVIVORS_FOR_ALL_SURVIVED_WARNING:
        return None
    files = sorted({m.path for m in run.survivors})
    where = files[0] if len(files) == 1 else ", ".join(files)
    return (
        f"warning: all {run.survived} evaluated mutants survived. This usually means the test "
        f"suite ran but never exercised {where} — check that --tests (or --command) targets it. "
        "The mutation score and exit code are unchanged."
    )


def console_summary(run: MutationRun) -> str:
    """A human-readable summary: score, per-verdict counts, and each survivor's location."""
    score = run.mutation_score
    score_str = "n/a" if score is None else f"{score * 100:.1f}%"
    lines = [
        f"Mutation score: {score_str}",
        f"  killed:   {run.killed}",
        f"  timeout:  {run.timeouts}  (counted as killed)",
        f"  survived: {run.survived}",
        f"  ignored:  {run.ignored}  (suppressed, excluded from score)",
        f"  invalid:  {run.invalid}",
        f"  error:    {run.errors}",
    ]
    if run.survivors:
        lines += ["", f"Survivors ({len(run.survivors)}):", ""]
        source_cache: dict[str, list[str] | None] = {}
        on_asserts = 0
        for m in run.survivors:
            if m.path not in source_cache:
                source_cache[m.path] = _read_source_lines(m.path)
            source_lines = source_cache[m.path]
            if reference_section(m, source_line(m, source_lines)) == ASSERT_SECTION:
                on_asserts += 1
            lines += render_survivor(m, source_lines)
            lines.append("")
        note = _assert_survivor_note(on_asserts, len(run.survivors))
        if note is not None:
            lines += [note, ""]
    return "\n".join(lines)


def _assert_survivor_note(on_asserts: int, survivors: int) -> str | None:
    """A one-line count of how many survivors sit inside an `assert`, or ``None`` when none do.

    Each such survivor already explains itself, but the *proportion* is the thing a reader can only
    see in aggregate — and it is what a score means. Defensive code can put most of a file's
    survivors on assert lines, and none of those are gaps anyone can close, so a healthy-looking
    percentage over a report with nothing actionable in it is exactly the reading to head off. This
    counts and links; it never hides a mutant or moves the score."""
    if not on_asserts:
        return None
    return (
        f"note: {on_asserts} of {survivors} survivors sit inside an assert, which no in-process "
        f"test can kill.\n  They are legitimate survivors, not gaps: {doc_url(ASSERT_SECTION)}"
    )


def _change_note(mutant: Mutant) -> str:
    """A plain-language, one-line rendering of what this survivor changed — the Markdown peer of the
    caret note `render_survivor` draws. The phrasing itself is `htmlreport.change_note`, shared so
    the Markdown summary and the HTML report can't describe the same mutant two different ways;
    only the backticks around the tokens are Markdown's own."""
    return change_note(
        mutant.operator_id,
        f"`{mutant.original}`",
        f"`{mutant.replacement}`" if mutant.replacement else "",
    )


def _survivor_markdown(mutant: Mutant, source_lines: list[str] | None) -> list[str]:
    """One survivor as Markdown lines: a heading (``path:line`` + operator), the source line in a
    fenced block with a plain note of the change, then the gap and the risk+start narrative, and the
    stable per-operator docs link. This is `render_survivor`'s content rendered as Markdown instead
    of box-drawing; the code slot drops out gracefully when the source is unreadable. The narrative
    is `survivor_report_fields` — the exact copy the console block and the Stryker JSON carry, so
    the three surfaces can never drift (including its assert handling: an assert survivor gets the
    assert explanation and the assert link here too, exactly as it does on the console)."""
    line_no = mutant.span.line
    src = source_line(mutant, source_lines)
    gap, risk_start = survivor_report_fields(mutant, src)
    out = [f"#### `{mutant.path}:{line_no}` · {mutant.operator_id}", ""]
    if src is not None:
        out += [
            "```gdscript",
            src.expandtabs(4),
            "```",
            "",
            f"{_change_note(mutant)} — every test still passed.",
            "",
        ]
    anchor = reference_section(mutant, src)
    label = (
        "survivors inside an `assert`"
        if anchor == ASSERT_SECTION
        else f"the `{mutant.operator_id}` operator"
    )
    out += [
        f"**The gap.** {gap}",
        "",
        "**Why it matters, and where to start.**",
        "",
        risk_start,
        "",
        f"[Explain {label}]({doc_url(anchor)})",
        "",
    ]
    return out


def job_summary_markdown(run: MutationRun) -> str:
    """Render `run` as GitHub-flavored Markdown for the Actions job summary
    (``$GITHUB_STEP_SUMMARY``): the score, the per-verdict tally, and — gdmutant's differentiator —
    each survivor's gap/risk/start *explanation*, not just its location. No established mutation
    tool surfaces the explanations in a GitHub view; this puts them right in the CI run, where
    reviewers look (the HTML artifact goes unclicked). Ends with a trailing newline so it appends
    cleanly to the summary file."""
    score = run.mutation_score
    score_str = "n/a" if score is None else f"{score * 100:.1f}%"
    out = [
        "## gdmutant — mutation report",
        "",
        f"**Mutation score: {score_str}**",
        "",
        f"{run.killed} killed · {run.timeouts} timeout · **{run.survived} survived** · "
        f"{run.ignored} ignored · {run.invalid} invalid · {run.errors} error",
        "",
    ]
    if not run.survivors:
        out.append("No surviving mutants — every mutant your tests could catch, they caught.")
        return "\n".join(out) + "\n"
    out += [
        f"### Surviving mutants ({len(run.survivors)})",
        "",
        "Each is a line a bug could live on that no test catches. gdmutant explains the gap — "
        "not just the location:",
        "",
    ]
    source_cache: dict[str, list[str] | None] = {}
    for mutant in run.survivors:
        if mutant.path not in source_cache:
            source_cache[mutant.path] = _read_source_lines(mutant.path)
        out += _survivor_markdown(mutant, source_cache[mutant.path])
    return "\n".join(out) + "\n"


def _read_source_lines(path: str) -> list[str] | None:
    """The source file's lines for the survivor's code/caret context, or ``None`` if unreadable.

    Files are restored to their original source by the time the summary is rendered, so this reads
    the real line the mutant sat on. A survivor whose file has since moved still gets its
    narrative — only the code + caret + enclosing-function slots are dropped."""
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
