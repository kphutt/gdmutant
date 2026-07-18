"""The mutation-run loop: select -> mutate -> run -> tally -> score.

For each mutant the adapter generates, the loop materializes it on disk, runs the target's test
suite via a `Runner`, and classifies the outcome:

* **killed** — the suite failed (a test caught the mutation),
* **survived** — the suite passed (no test caught it),
* **timeout** — the suite exceeded its time budget: a mutation-induced hang (infinite loop) *is* a
  detection, so it counts as killed (Stryker's ``Timeout`` status),
* **ignored** — a ``# gdmutant: ignore`` annotation suppresses it (an equivalent/unkillable mutant);
  generated for the report but never run, and excluded from the score (Stryker's ``Ignored``),
* **invalid** — the mutant didn't parse (NF-5); never counted as killed, never run,
* **error** — the runner failed to execute the mutant (e.g. a Godot crash); tallied so one bad run
  doesn't discard the whole pass, and excluded from the score.

no-coverage is folded into *survived* for v0.1 (DESIGN.md FG-4.1) until coverage-gating lands. Each
mutant is applied in isolation and the original restored in a ``finally`` — see docs/decisions/0003.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gdmutant.adapters.gdscript import apply_mutant, generate_mutants
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import CATALOG, Operator
from gdmutant.engine.runner import Runner, SuiteTimeout

# Per-mutant timeout derived from the baseline's wall-clock, so a hanging mutant is cut off in
# seconds rather than blocking for a flat default (the #1 first-run "looks frozen" complaint).
_MIN_TIMEOUT = 10.0  # floor: never rule a mutant a hang in under 10s (absorbs jitter/cold caches)
_TIMEOUT_FACTOR = 10.0  # a mutant gets 10x the baseline's time before it's a hang
_MAX_DERIVED_TIMEOUT = 600.0  # cap: a slow suite is never worse off than the historical default


def _derive_timeout(baseline_secs: float) -> float:
    """Per-mutant timeout from the baseline run time: ``baseline * factor``, floored and capped."""
    return min(_MAX_DERIVED_TIMEOUT, max(_MIN_TIMEOUT, baseline_secs * _TIMEOUT_FACTOR))


class BaselineFailed(Exception):
    """The unmutated suite failed — mutation testing a red suite is meaningless (FG-3.3)."""


class Verdict(Enum):
    KILLED = "killed"
    SURVIVED = "survived"
    TIMEOUT = "timeout"
    IGNORED = "ignored"
    INVALID = "invalid"
    ERROR = "error"


@dataclass(frozen=True)
class MutantOutcome:
    mutant: Mutant
    verdict: Verdict


@dataclass(frozen=True)
class MutationRun:
    """The tally of a mutation run — outcomes in the order mutants were generated (NF-1)."""

    outcomes: tuple[MutantOutcome, ...]

    def _count(self, verdict: Verdict) -> int:
        return sum(1 for o in self.outcomes if o.verdict is verdict)

    @property
    def killed(self) -> int:
        return self._count(Verdict.KILLED)

    @property
    def survived(self) -> int:
        return self._count(Verdict.SURVIVED)

    @property
    def timeouts(self) -> int:
        return self._count(Verdict.TIMEOUT)

    @property
    def ignored(self) -> int:
        return self._count(Verdict.IGNORED)

    @property
    def invalid(self) -> int:
        return self._count(Verdict.INVALID)

    @property
    def errors(self) -> int:
        return self._count(Verdict.ERROR)

    @property
    def survivors(self) -> tuple[Mutant, ...]:
        """The mutants no test caught — the product's core output."""
        return tuple(o.mutant for o in self.outcomes if o.verdict is Verdict.SURVIVED)

    @property
    def detected(self) -> int:
        """Mutants a test caught — killed outright *or* by hanging the suite (timeout)."""
        return self.killed + self.timeouts

    @property
    def mutation_score(self) -> float | None:
        """``detected / (detected + survived)`` where detected = killed + timeouts; ``None`` when
        there are no killable mutants. Timeouts count as detected (Stryker convention): a mutation
        that hangs the suite was observably caught. invalid/error are excluded entirely."""
        scored = self.detected + self.survived
        return self.detected / scored if scored else None


def _progress_start(index: int, total: int, mutant: Mutant, timeout: float) -> str:
    """Heartbeat emitted *before* a mutant runs: shows which mutant is running and its time budget,
    so a slow or hanging mutant reads as "running mutant N (<=Ns)" rather than a frozen terminal —
    the #1 first-run complaint. The verdict line follows once it resolves.
    """
    loc = f"{mutant.path}:{mutant.span.line}:{mutant.span.column}"
    head = f"[{index}/{total}] {loc}  {mutant.describe_change()}"
    return f"{head}  running (<={timeout:g}s) ..."


def _format_duration(secs: float) -> str:
    """A compact human duration: ``9s``, ``2m 15s``, ``1h 3m``. Rounds to whole seconds."""
    whole = int(round(secs))
    if whole < 60:
        return f"{whole}s"
    minutes, s = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {s}s"
    hours, m = divmod(minutes, 60)
    return f"{hours}h {m}m"


def _progress_estimate(runnable: int, total: int, baseline_secs: float) -> str:
    """The pre-run estimate line: how many mutants will run and roughly how long, derived from the
    baseline's own wall-clock. "Looks hung" is the #1 documented reason people abandon mutation
    testing ([ticket]); a stated ETA makes a long run read as *expected*. Rough by construction —
    each mutant is budgeted at ~the baseline time, and killed mutants often finish sooner — so it's
    an upper-ish "about" figure, not a promise. Ignored mutants never run, so they're excluded from
    the time but noted in the count."""
    est = _format_duration(runnable * baseline_secs)
    ignored = total - runnable
    ignored_note = f" ({ignored} ignored, not run)" if ignored else ""
    unit = "mutant" if total == 1 else "mutants"
    return f"{total} {unit}{ignored_note}; baseline ~{baseline_secs:.1f}s each → estimated ≈ {est}"


def _progress_line(index: int, total: int, outcome: MutantOutcome) -> str:
    """One human-readable progress line for a finished mutant.

    Format: ``[i/N] path:line:col  original -> replacement  ... verdict`` — which mutant just
    resolved and how. The engine formats it; the caller (the CLI) decides where it goes — always
    stderr, so ``--json -`` keeps stdout pure JSON.
    """
    m = outcome.mutant
    loc = f"{m.path}:{m.span.line}:{m.span.column}"
    return f"[{index}/{total}] {loc}  {m.describe_change()}  ... {outcome.verdict.value}"


def run(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    catalog: tuple[Operator, ...] = CATALOG,
    *,
    timeout: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> MutationRun:
    """Run the full mutation pass over `source` (the contents of `path`) using `runner`.

    Raises `BaselineFailed` if the unmutated suite doesn't pass first (FG-3.3). The file at `path`
    must hold `source` when this is called; it is restored to `source` before returning.

    `timeout` is the per-mutant time budget in seconds. When ``None`` (the default), it is *derived
    from the baseline run's own wall-clock* (`_derive_timeout`), so a hanging mutant is cut off in
    seconds instead of blocking for the flat default — an explicit value overrides the derivation.

    If `progress` is given, it is called with the baseline notice, then — for each mutant that
    actually runs — a "running (<=Ns)" heartbeat *before* it runs and a verdict line *after*, in
    generation order (invalid mutants never run, so they get only the verdict line). The CLI wires
    it to stderr so a long or hanging run shows steady output instead of looking frozen; `None`
    runs silently.
    """
    if progress is not None:
        progress("running the unmutated (baseline) suite ...")
    started = time.monotonic()
    try:
        baseline = runner.run(project_dir)
    except Exception as error:  # a runner that can't even run the unmutated suite is a setup error
        raise BaselineFailed(
            f"could not run the unmutated suite for {project_dir!r}: {error}"
        ) from error
    baseline_secs = time.monotonic() - started
    if baseline.failed:
        detail = f":\n{baseline.detail}" if baseline.detail else ""
        raise BaselineFailed(f"the unmutated test suite failed for {project_dir!r}{detail}")

    per_mutant_timeout = timeout if timeout is not None else _derive_timeout(baseline_secs)
    outcomes: list[MutantOutcome] = []
    mutants = generate_mutants(path, source, catalog)
    total = len(mutants)
    if progress is not None:
        runnable = sum(1 for m in mutants if m.ignore_reason is None)
        progress(_progress_estimate(runnable, total, baseline_secs))
    for index, mutant in enumerate(mutants, start=1):
        if mutant.ignore_reason is not None:
            # A `# gdmutant: ignore` annotation suppresses this mutant — generated for the report
            # but never run (no validity check, no suite run): tallied IGNORED, excluded from score.
            verdict = Verdict.IGNORED
        else:
            mutated, valid = apply_mutant(mutant, source)
            if valid:
                if progress is not None:
                    progress(_progress_start(index, total, mutant, per_mutant_timeout))
                verdict = _run_one(project_dir, path, source, mutated, runner, per_mutant_timeout)
            else:
                verdict = Verdict.INVALID
        outcome = MutantOutcome(mutant, verdict)
        outcomes.append(outcome)
        if progress is not None:
            progress(_progress_line(index, total, outcome))
    return MutationRun(tuple(outcomes))


def _run_one(
    project_dir: str, path: str, source: str, mutated: str, runner: Runner, timeout: float
) -> Verdict:
    target = Path(path)
    try:
        target.write_text(mutated, encoding="utf-8")
        try:
            result = runner.run(project_dir, timeout=timeout)
        except SuiteTimeout:
            # The mutation hung the suite — a detection, not a crash. Count it as killed
            # (Stryker's Timeout status), distinct from ERROR below.
            return Verdict.TIMEOUT
        except Exception:
            # The runner failed to execute this mutant (e.g. a Godot crash). Tally it as ERROR and
            # carry on — one bad run must not discard the whole pass (FG-4.1).
            return Verdict.ERROR
        return Verdict.KILLED if result.failed else Verdict.SURVIVED
    finally:
        target.write_text(source, encoding="utf-8")  # never leave the project mutated
