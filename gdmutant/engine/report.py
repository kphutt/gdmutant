"""Reporting: a console summary + the Stryker mutation-testing-elements JSON report (FG-5).

The JSON follows the mutation-testing-elements report schema (v2), so it renders in that
ecosystem's HTML viewer. Verdicts map to the schema's ``MutantStatus``:

    killed -> Killed, survived -> Survived, timeout -> Timeout, invalid -> CompileError,
    error -> RuntimeError.

Locations are 1-based line + column with an exclusive end column, which is exactly what a
`Span` carries, so no coordinate conversion is needed.
"""

from __future__ import annotations

from typing import Any

from gdmutant.engine.loop import MutationRun, Verdict

SCHEMA_VERSION = "2"

_STATUS: dict[Verdict, str] = {
    Verdict.KILLED: "Killed",
    Verdict.SURVIVED: "Survived",
    Verdict.TIMEOUT: "Timeout",
    Verdict.INVALID: "CompileError",
    Verdict.ERROR: "RuntimeError",
}


def stryker_report(run: MutationRun, path: str, source: str, language: str) -> dict[str, Any]:
    """Build the mutation-testing-elements report dict for a single-file `run`.

    `language` is supplied by the caller (the adapter/CLI knows it) — the reporter stays
    language-neutral and carries no default.
    """
    mutants: list[dict[str, Any]] = [
        {
            "id": str(index),
            "mutatorName": outcome.mutant.operator_id,
            "replacement": outcome.mutant.replacement,
            "location": {
                "start": {"line": outcome.mutant.span.line, "column": outcome.mutant.span.column},
                "end": {
                    "line": outcome.mutant.span.end_line,
                    "column": outcome.mutant.span.end_column,
                },
            },
            "status": _STATUS[outcome.verdict],
        }
        for index, outcome in enumerate(run.outcomes)
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "thresholds": {"high": 80, "low": 60},
        "files": {path: {"language": language, "source": source, "mutants": mutants}},
    }


def console_summary(run: MutationRun) -> str:
    """A human-readable summary: score, per-verdict counts, and each survivor's location."""
    score = run.mutation_score
    score_str = "n/a" if score is None else f"{score * 100:.1f}%"
    lines = [
        f"Mutation score: {score_str}",
        f"  killed:   {run.killed}",
        f"  timeout:  {run.timeouts}  (counted as killed)",
        f"  survived: {run.survived}",
        f"  invalid:  {run.invalid}",
        f"  error:    {run.errors}",
    ]
    if run.survivors:
        lines += ["", f"Survivors ({len(run.survivors)}):"]
        for m in run.survivors:
            loc = f"{m.path}:{m.span.line}:{m.span.column}"
            lines.append(f"  {loc}  {m.operator_id}  {m.original} -> {m.replacement}")
    return "\n".join(lines)
