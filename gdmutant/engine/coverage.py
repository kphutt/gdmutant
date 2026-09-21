"""Coverage analysis: which mutants no test reaches (docs/decisions/0017, step 2).

Before any mutant runs, a language adapter marks a throwaway copy of the project: one small call in
front of each mutation spot, which records "spot N was reached" when the suite runs. The engine runs
the whole suite once on that copy (the marker run), reads back which spots were reached, and a
mutant whose spot was never reached gets the `no coverage` verdict without its suite ever running.
Until that spot runs, the mutated program does exactly what the original did, so no test that
never reaches it can fail because of it.

That argument only holds if the marker run is trustworthy, so the run must be clean
(`clean_run_problems`), and a few no-coverage mutants are also run for real as a standing check
(`self_check_sample`). This module holds the language-neutral pieces: the option, the contract a
language adapter implements (`Marker`), the hits file, the clean-run rules, and the sample. It
never names a language: it sees spot numbers, paths and "run everything" placements, nothing else.
The loop (`engine.loop`) runs the pieces in order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from gdmutant.engine.mutants import Mutant
from gdmutant.engine.runner import SuiteResult


class CoverageAnalysis(Enum):
    """The ``--coverage-analysis`` option. The names follow Stryker's ``coverageAnalysis``."""

    #: No markers. Every mutant runs the whole suite. The default, and what gdmutant always did.
    OFF = "off"
    #: Markers, and the `no coverage` verdict. Every mutant that some test reaches still runs the
    #: whole suite.
    ALL = "all"
    #: Markers, and each mutant runs only the test files that reach it. Not built yet
    #: (docs/decisions/0017, step 3), so asking for it is refused rather than quietly downgraded.
    PER_FILE = "per-file"


#: How many no-coverage mutants the self-check re-runs against the whole suite by default. A few,
#: per the ADR: the sample is a tripwire for a broken map, not proof of a sound one.
SELF_CHECK_SAMPLE = 3


@dataclass(frozen=True)
class MarkedCopy:
    """What a `Marker` hands back after marking a copy of the project.

    `hits_path` is where the recorder writes the reached spots when the suite's process exits.
    `placements` maps each marked file (by the same project-relative path it was given) to one entry
    per mutant, in the order the mutants were given: the id of the spot whose marker covers it, or
    ``None`` when no marker can, which means the mutant always runs the whole suite.
    """

    hits_path: str
    placements: Mapping[str, tuple[int | None, ...]]


class Marker(Protocol):
    """The language-specific half of coverage analysis: put markers into a copy of a project."""

    def mark(self, copy_dir: str, files: Mapping[str, tuple[str, Sequence[Mutant]]]) -> MarkedCopy:
        """Mark the project copy at `copy_dir` and install the recorder that writes the hits file.

        `files` maps each file to mark, by its path relative to `copy_dir`, to its unmarked source
        and its mutants. Raises on anything that stops the copy from being marked (a name the
        recorder needs is already taken, the language tool could not register it, ...), with a
        message that says what to do."""
        ...


class HitsUnreadable(Exception):
    """The hits file is missing, or holds something other than a list of spot numbers."""


def read_hits(path: Path) -> frozenset[int]:
    """The spot ids recorded in the hits file at `path`: a JSON object ``{"hits": [ids]}``.

    Raises `HitsUnreadable` when the file is missing or malformed. Neither may ever read as
    "nothing was reached": a crash at exit that loses the file would otherwise turn every mutant
    into "no coverage", which is exactly the wrong direction.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HitsUnreadable(
            f"the recorder wrote no hits file at {path}. It writes one when the suite's Godot "
            "process exits normally, so the run crashed, was killed, or ran a different project "
            "directory than the marked copy it was started in (a --command that names the project "
            "by an absolute path instead of --path . does that)"
        ) from None
    except (OSError, ValueError) as error:
        raise HitsUnreadable(f"the hits file at {path} could not be read: {error}") from None
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list) or not all(
        isinstance(spot, int) and not isinstance(spot, bool) for spot in hits
    ):
        raise HitsUnreadable(
            f"the hits file at {path} is not a list of spot numbers: {str(data)[:200]}"
        )
    return frozenset(hits)


def clean_run_problems(
    result: SuiteResult, baseline_tests: int, hits: frozenset[int] | HitsUnreadable
) -> list[str]:
    """Every reason the marker run cannot be trusted, in plain words, or an empty list.

    The rules are the ADR's "The marker run must be clean" for step 2, and all of them are checked,
    so a run that fails two says both. Each guards a way the map can say "unreached" about code a
    test reaches: a failing test may have stopped before the spot, a script error cuts a function
    short, a lost hits file or a recorder that never fired says nothing about anything, and a
    different test count means the marker run was not the suite the baseline ran.
    """
    problems: list[str] = []
    if result.failed:
        detail = f"\n{result.detail}" if result.detail else ""
        problems.append(
            f"not every test passed in the marker run ({result.failures} failed, "
            f"{result.errors} errored), though the same suite passed without markers{detail}"
        )
    if result.runtime_error:
        problems.append(
            "the marker run's output holds a runtime error, which aborts the function it happens "
            f"in and can make code a test reaches look unreached:\n{result.runtime_error}"
        )
    if result.tests != baseline_tests:
        problems.append(
            f"the marker run ran {result.tests} tests, but the baseline ran {baseline_tests}"
        )
    if isinstance(hits, HitsUnreadable):
        problems.append(str(hits))
    elif not hits:
        problems.append(
            "no marker recorded a single hit, so the recorder never ran. A suite that passed "
            "must reach some of the code it tests"
        )
    return problems


def uncovered(placements: Sequence[int | None], hits: frozenset[int]) -> frozenset[int]:
    """The indexes, into `placements`, of the mutants whose spot no test reached.

    A ``None`` placement is never "no coverage": no marker could say whether it runs, so it
    always runs the whole suite."""
    return frozenset(
        index for index, spot in enumerate(placements) if spot is not None and spot not in hits
    )


def _sample_key(path: str, mutant: Mutant) -> str:
    """A stable hash of one mutant, independent of where the project sits on disk."""
    text = (
        f"{Path(path).as_posix()}:{mutant.span.line}:{mutant.span.column}:"
        f"{mutant.operator_id}:{mutant.replacement}"
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def self_check_sample(
    candidates: Sequence[tuple[str, int, Mutant]], size: int | None
) -> frozenset[tuple[str, int]]:
    """Which no-coverage mutants to also run against the whole suite: ``(path, index)`` pairs.

    `candidates` are ``(project-relative path, index, mutant)`` for every no-coverage mutant.
    `size` is how many to take, or ``None`` for all of them. The choice is by a stable hash, so the
    same project picks the same mutants every run, and whenever there is at least one candidate at
    least one is taken, as the ADR requires. A size below one is raised to one for that reason.
    """
    ordered = sorted(candidates, key=lambda c: _sample_key(c[0], c[2]))
    take = len(ordered) if size is None else max(size, 1)
    return frozenset((path, index) for path, index, _ in ordered[:take])
