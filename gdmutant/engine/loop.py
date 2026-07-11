"""The mutation-run loop: select -> mutate -> run -> tally -> score.

For each mutant the adapter generates, the loop materializes it on disk, runs the target's test
suite via a `Runner`, and classifies the outcome:

* **killed** — the suite failed (a test caught the mutation),
* **survived** — the suite passed (no test caught it),
* **invalid** — the mutant didn't parse (NF-5); never counted as killed, never run,
* **error** — the runner failed to execute the mutant (e.g. a Godot crash/timeout); tallied so
  one bad run doesn't discard the whole pass, and excluded from the score.

no-coverage is folded into *survived* for v0.1 (DESIGN.md FG-4.1) until coverage-gating lands. Each
mutant is applied in isolation and the original restored in a ``finally`` — see docs/decisions/0003.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gdmutant.adapters.gdscript import apply_mutant, generate_mutants
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import CATALOG, Operator
from gdmutant.engine.runner import Runner


class BaselineFailed(Exception):
    """The unmutated suite failed — mutation testing a red suite is meaningless (FG-3.3)."""


class Verdict(Enum):
    KILLED = "killed"
    SURVIVED = "survived"
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
    def mutation_score(self) -> float | None:
        """``killed / (killed + survived)``; ``None`` when there are no killable mutants."""
        scored = self.killed + self.survived
        return self.killed / scored if scored else None


def run(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    catalog: tuple[Operator, ...] = CATALOG,
) -> MutationRun:
    """Run the full mutation pass over `source` (the contents of `path`) using `runner`.

    Raises `BaselineFailed` if the unmutated suite doesn't pass first (FG-3.3). The file at `path`
    must hold `source` when this is called; it is restored to `source` before returning.
    """
    try:
        baseline = runner.run(project_dir)
    except Exception as error:  # a runner that can't even run the unmutated suite is a setup error
        raise BaselineFailed(
            f"could not run the unmutated suite for {project_dir!r}: {error}"
        ) from error
    if baseline.failed:
        raise BaselineFailed(f"the unmutated test suite failed for {project_dir!r}")

    outcomes: list[MutantOutcome] = []
    for mutant in generate_mutants(path, source, catalog):
        mutated, valid = apply_mutant(mutant, source)
        if not valid:
            outcomes.append(MutantOutcome(mutant, Verdict.INVALID))
            continue
        verdict = _run_one(project_dir, path, source, mutated, runner)
        outcomes.append(MutantOutcome(mutant, verdict))
    return MutationRun(tuple(outcomes))


def _run_one(project_dir: str, path: str, source: str, mutated: str, runner: Runner) -> Verdict:
    target = Path(path)
    try:
        target.write_text(mutated, encoding="utf-8")
        try:
            result = runner.run(project_dir)
        except Exception:
            # The runner failed to execute this mutant (e.g. a Godot crash/timeout). Tally it as
            # ERROR and carry on — one bad run must not discard the whole pass (FG-4.1).
            return Verdict.ERROR
        return Verdict.KILLED if result.failed else Verdict.SURVIVED
    finally:
        target.write_text(source, encoding="utf-8")  # never leave the project mutated
