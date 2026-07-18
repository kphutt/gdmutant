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

from typing import Any

from gdmutant.engine.loop import MutantOutcome, MutationRun, Verdict

#: One actionable "how to kill it" hint per operator id — a survivor list otherwise reads as
#: homework ([ticket]). Keyed by ``operator_id``; ``_kill_hint`` falls back to a generic line for any
#: operator not listed (custom catalog). The doc `docs/reading-your-first-report.md` goes deeper.
_KILL_HINTS: dict[str, str] = {
    "comparison": "test the boundary value where the two operators differ (equal inputs)",
    "boolean": "test operands that disagree (one true, one false) so and vs or matters",
    "arithmetic": "assert the exact result, not just its sign or 'nonzero'",
    "constant": "test an outcome that depends on this boolean's actual value",
    "numeric": "pin the exact value/boundary this literal controls",
    "compound-assign": "assert the accumulated value after this update",
    "modulo": "test a non-multiple input where % differs from * and /",
    "logical-not": "exercise the guarded branch with the condition both ways",
    "statement-deletion": "assert an effect that fails when this statement is removed",
}
_GENERIC_KILL_HINT = "write a test that fails under this exact change"


def _kill_hint(operator_id: str) -> str:
    """A one-line, human-actionable suggestion for killing a survivor of `operator_id`."""
    return _KILL_HINTS.get(operator_id, _GENERIC_KILL_HINT)


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


def stryker_report(run: MutationRun, path: str, source: str, language: str) -> dict[str, Any]:
    """Build the mutation-testing-elements report dict for a single-file `run`.

    `language` is supplied by the caller (the adapter/CLI knows it) — the reporter stays
    language-neutral and carries no default.
    """
    mutants = [_mutant_json(index, outcome) for index, outcome in enumerate(run.outcomes)]
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
        f"  ignored:  {run.ignored}  (suppressed, excluded from score)",
        f"  invalid:  {run.invalid}",
        f"  error:    {run.errors}",
    ]
    if run.survivors:
        lines += ["", f"Survivors ({len(run.survivors)}):"]
        for m in run.survivors:
            loc = f"{m.path}:{m.span.line}:{m.span.column}"
            lines.append(f"  {loc}  {m.operator_id}  {m.describe_change()}")
            lines.append(f"      → kill it: {_kill_hint(m.operator_id)}")
    return "\n".join(lines)
