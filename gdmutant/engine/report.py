"""Reporting: a console summary + the Stryker mutation-testing-elements JSON report (FG-5).

The JSON follows the mutation-testing-elements report schema (v2), so it renders in that
ecosystem's HTML viewer. Verdicts map to the schema's ``MutantStatus``:

    killed -> Killed, survived -> Survived, timeout -> Timeout, ignored -> Ignored,
    invalid -> CompileError, error -> RuntimeError.

A suppressed (ignored) mutant carries its ``# gdmutant: ignore`` reason as the schema's optional
``statusReason`` field, so the viewer shows *why* it was ignored.

Locations are 1-based line + column with an exclusive end column, which is exactly what a
`Span` carries, so no coordinate conversion is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gdmutant.engine.explain import render_survivor
from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict

SCHEMA_VERSION = "2"

_STATUS: dict[Verdict, str] = {
    Verdict.KILLED: "Killed",
    Verdict.SURVIVED: "Survived",
    Verdict.TIMEOUT: "Timeout",
    Verdict.IGNORED: "Ignored",
    Verdict.INVALID: "CompileError",
    Verdict.ERROR: "RuntimeError",
}


def _mutant_json(index: int, outcome: MutantOutcome) -> dict[str, Any]:
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
    return mutant


def _file_entry(run: MutationRun, source: str, language: str) -> dict[str, Any]:
    """One file's entry in the report's `files` map."""
    return {
        "language": language,
        "source": source,
        "mutants": [_mutant_json(index, outcome) for index, outcome in enumerate(run.outcomes)],
    }


def stryker_report_multi(
    files: dict[str, tuple[MutationRun, str]], language: str
) -> dict[str, Any]:
    """Build a merged mutation-testing-elements report for one or more files (LOD-79). `files` maps
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


#: The pinned mutation-testing-elements viewer version (kept in sync with the README recipe).
_HTML_VIEWER_VERSION = "3.8.4"


def html_report(report: dict[str, Any]) -> str:
    """A ready-to-open HTML report (LOD-103): the standard mutation-testing-elements viewer with the
    Stryker `report` dict inlined, so ``gdmutant run --html out.html`` yields **one file you
    double-click** — no manual viewer wiring. This automates the ``view.html`` recipe the README
    documents; the interactive viewer itself loads from a pinned CDN (needs network to *render*, not
    to save). The JSON rides in a non-executable ``<script type="application/json">`` block with
    ``</`` escaped to ``<\\/`` — valid JSON (``\\/`` escapes ``/``) that a ``</script>`` inside
    GDScript source can't use to break out of the tag; the viewer reads it back at load."""
    data = json.dumps(report).replace("</", "<\\/")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><title>gdmutant mutation report</title></head>\n'
        "<body>\n"
        "<mutation-test-report-app></mutation-test-report-app>\n"
        f'<script src="https://www.unpkg.com/mutation-testing-elements@{_HTML_VIEWER_VERSION}">'
        "</script>\n"
        f'<script type="application/json" id="mutation-test-report">{data}</script>\n'
        "<script>\n"
        '  document.querySelector("mutation-test-report-app").report =\n'
        '    JSON.parse(document.getElementById("mutation-test-report").textContent);\n'
        "</script>\n"
        "</body>\n"
        "</html>\n"
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
        for m in run.survivors:
            if m.path not in source_cache:
                source_cache[m.path] = _read_source_lines(m.path)
            lines += render_survivor(m, source_cache[m.path])
            lines.append("")
    return "\n".join(lines)


def _read_source_lines(path: str) -> list[str] | None:
    """The source file's lines for the survivor's code/caret context, or ``None`` if unreadable.

    Files are restored to their original source by the time the summary is rendered, so this reads
    the real line the mutant sat on. A survivor whose file has since moved still gets its
    narrative — only the code + caret + enclosing-function slots are dropped."""
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
