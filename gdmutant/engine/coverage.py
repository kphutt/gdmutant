"""Coverage analysis: which mutants no test reaches, and which tests reach the rest (ADR 0017).

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

Step 3 adds selection (``--coverage-analysis per-file``). The recorder now files each hit under the
test file that was running when it happened, so the map says not only *whether* some test reaches a
spot but *which* tests do, and a mutant runs only those. Two passes build it, forward and then in
reverse file order, and only a spot both passes agree about is trusted (`build_map`). Everything
else, and everything reached with no test file running, runs the whole suite exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    #: Markers, and each mutant runs only the test files that reach it (docs/decisions/0017,
    #: step 3). Needs a runner that can run a named list of test files
    #: (`engine.runner.FileSelecting`); one that cannot is refused rather than quietly downgraded.
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
    `recorder_dir` is where in the copy the adapter put the recorder, which is where a runner
    installs its file-window hook (`engine.runner.FileSelecting.install_windows`).
    """

    hits_path: str
    placements: Mapping[str, tuple[int | None, ...]]
    recorder_dir: str = ""


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


#: The key the recorder files a hit under when no test file was running: load time, the gap between
#: two files, or after the last one. It is deliberately a value a framework can never produce as a
#: file name, so it cannot collide with a real window.
LOAD_TIME = ""


@dataclass(frozen=True)
class Hits:
    """One marker pass, read back from the recorder's hits file.

    `spots` is every spot reached anywhere in the pass, which is all
    ``--coverage-analysis all`` ever needs. The rest is what selection adds (step 3): `windows`
    maps each test file to the spots reached while it was running, `load_time` holds the spots
    reached while no test file was, and `opened` lists the test files that opened a window, in the
    order they ran, once per suite it ran, so a file holding several suites appears several times
    (`files` is the distinct list). A file that opened a window and reached nothing is still in both
    `windows` (with an empty set) and `opened`, so "the hook never fired" and "this file reaches
    nothing" stay distinguishable.
    """

    spots: frozenset[int]
    windows: Mapping[str, frozenset[int]] = field(default_factory=dict)
    load_time: frozenset[int] = frozenset()
    opened: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        """The distinct test files, in the order they first ran.

        A framework may run several suites out of one file: GUT treats every inner class of a test
        script as a suite of its own, so one file opens several windows in a row. The file is still
        one file to hand back on a command line, and one file to be credited with a hit, so
        everything outside `opened` itself works from this.
        """
        return tuple(dict.fromkeys(self.opened))


#: A default that is not ``None``, since ``None`` is a value a hits file can really hold.
_UNSET: object = object()


def _spot_list(value: object, path: Path, what: str, quote: object = _UNSET) -> frozenset[int]:
    """`value` as a set of spot ids, or raise `HitsUnreadable` naming `what` went wrong.

    `quote` is what the message shows when it does, which is the whole file for the top-level list
    (where a missing key would otherwise be reported as the word "None") and the offending value
    itself for a list nested inside it.
    """
    if not isinstance(value, list) or not all(
        isinstance(spot, int) and not isinstance(spot, bool) for spot in value
    ):
        shown = value if quote is _UNSET else quote
        raise HitsUnreadable(f"the hits file at {path} {what}: {str(shown)[:200]}")
    return frozenset(value)


def read_hits(path: Path) -> Hits:
    """The hits file at `path`, as a `Hits`.

    The file is a JSON object the recorder writes when the suite's process exits: ``hits`` is every
    spot reached, ``windows`` maps a test file to the spots reached while it ran, and ``opened``
    lists the test files whose window opened. Only ``hits`` is required, so a recorder that never
    opened a window reads as a pass with no windows rather than as a broken file. The rule that
    catches a hook that never fired is `window_problems`, which says so in words.

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
    if not isinstance(data, dict):
        raise HitsUnreadable(f"the hits file at {path} is not a JSON object: {str(data)[:200]}")
    spots = _spot_list(data.get("hits"), path, "is not a list of spot numbers", quote=data)
    raw_windows = data.get("windows", {})
    if not isinstance(raw_windows, dict) or not all(isinstance(k, str) for k in raw_windows):
        raise HitsUnreadable(
            f"the hits file at {path} does not map test files to spot numbers: "
            f"{str(raw_windows)[:200]}"
        )
    windows = {
        name: _spot_list(value, path, f"records a bad spot list for {name!r}")
        for name, value in raw_windows.items()
        if name != LOAD_TIME
    }
    opened = data.get("opened", [])
    if not isinstance(opened, list) or not all(isinstance(name, str) for name in opened):
        raise HitsUnreadable(
            f"the hits file at {path} does not list the test files that ran: {str(opened)[:200]}"
        )
    return Hits(
        spots=spots,
        windows=windows,
        load_time=_spot_list(
            raw_windows.get(LOAD_TIME, []), path, "records a bad load-time spot list"
        ),
        opened=tuple(opened),
    )


def clean_run_problems(
    result: SuiteResult, baseline_tests: int, hits: Hits | HitsUnreadable
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
    elif not hits.spots:
        problems.append(
            "no marker recorded a single hit, so the recorder never ran. A suite that passed "
            "must reach some of the code it tests"
        )
    return problems


def window_problems(hits: Hits, result: SuiteResult) -> list[str]:
    """Every reason the marker pass cannot be attributed to test files, or an empty list.

    The ADR's two window rules, which only selection reads, so they are checked only in the pass
    that builds a per-file map. Both guard the same silence: a "a test file started" hook that
    never fires leaves every spot looking like load-time code or like code nothing reaches, and
    nothing else in the run says a word about it.

    The second rule compares counts, not names. The recorder gets a file's name from the framework's
    event and the report gets it from the framework's reporter, and the two need not spell it the
    same way, so comparing the names would fail on a cosmetic difference while comparing how many
    there are catches the case that matters, a file that ran without its window opening.
    """
    problems: list[str] = []
    if not hits.opened:
        problems.append(
            "no test file opened a coverage window, so the runner's 'a test file started' hook "
            "never fired. Without it gdmutant cannot tell which test file reaches which line"
        )
    elif len(hits.opened) != len(result.suites):
        problems.append(
            f"{len(hits.opened)} test files opened a coverage window, but the run's own report "
            f"describes {len(result.suites)}. A test file that runs without opening a window is "
            "credited to no test, so a mutant on a line only it reaches would be run against the "
            "wrong tests"
        )
    return problems


def uncovered(placements: Sequence[int | None], hits: frozenset[int]) -> frozenset[int]:
    """The indexes, into `placements`, of the mutants whose spot no test reached.

    A ``None`` placement is never "no coverage": no marker could say whether it runs, so it
    always runs the whole suite."""
    return frozenset(
        index for index, spot in enumerate(placements) if spot is not None and spot not in hits
    )


#: What a spot maps to when every test file must run for it: code that ran at load time or between
#: files, or a spot the two marker passes disagreed about. Spelled out rather than written as a bare
#: ``None`` at each call site, since "run everything" and "no test reaches this" are opposite
#: answers and both are falsy.
RUN_EVERYTHING = None

#: A spot's test files, or `RUN_EVERYTHING`. A spot missing from a map entirely is unreached.
SpotFiles = frozenset[str] | None


@dataclass(frozen=True)
class CoverageMap:
    """Which test files reach each marked spot, from both marker passes.

    `files` holds an entry for every spot either pass recorded: the test files that reach it, or
    `RUN_EVERYTHING`. A spot neither pass recorded is absent, which is "no test reaches it".
    `order_dependent` is the spots that are `RUN_EVERYTHING` because the two passes disagreed about
    them, counted apart in the run's summary because they are a fact about the suite, not about the
    code. `test_files` is every file that opened a window in the forward pass, which is the whole
    suite as the framework itself ran it.
    """

    files: Mapping[int, SpotFiles]
    order_dependent: frozenset[int]
    test_files: tuple[str, ...]

    def select(self, spot: int | None) -> SpotFiles:
        """The test files to run for a mutant placed at `spot`, or `RUN_EVERYTHING`.

        A mutant with no spot (`None`: the placement table could not mark it) runs everything, and
        so does one whose spot no test reached. The second is not a contradiction with the
        `no coverage` verdict: a caller decides that first, from `uncovered`, and only asks this
        about mutants it is going to run. Answering "everything" for a spot this map has never heard
        of is the safe answer to a question that should not have been asked.
        """
        if spot is None:
            return RUN_EVERYTHING
        return self.files.get(spot, RUN_EVERYTHING)


def build_map(forward: Hits, reverse: Hits) -> CoverageMap:
    """The union of two marker passes, as a `CoverageMap`.

    A spot's file set is trusted only when both passes agree on it exactly. Three things make a
    spot `RUN_EVERYTHING` instead:

    * It was reached while no test file was running. That is Stryker's static-mutant rule: code
      that runs at load time, between files or after the summary belongs to no test, so every test
      has to run. There is deliberately no option to skip such a mutant.
    * The two passes credit it to different files. Deferred work that fires after the test file that
      started it has ended lands in a *later* file, and running the files backwards makes "later" a
      different file (or no file at all), so the sets cannot match. The same disagreement is what a
      flaky test looks like, and it wants the same answer.
    * Only one pass reached it at all, which is the same disagreement with one side empty.
    """
    files: dict[int, SpotFiles] = {}
    order_dependent: set[int] = set()
    for spot in forward.spots | reverse.spots:
        if spot in forward.load_time or spot in reverse.load_time:
            files[spot] = RUN_EVERYTHING
            continue
        ahead = frozenset(name for name, spots in forward.windows.items() if spot in spots)
        behind = frozenset(name for name, spots in reverse.windows.items() if spot in spots)
        if ahead != behind:
            files[spot] = RUN_EVERYTHING
            order_dependent.add(spot)
        else:
            # An empty set here would mean "run no tests at all", which is never an answer: a spot
            # some pass recorded, credited to no file and not to load time, is a recorder that
            # contradicts itself. Say everything, the one answer that cannot lose a kill.
            files[spot] = ahead or RUN_EVERYTHING
    return CoverageMap(
        files=files, order_dependent=frozenset(order_dependent), test_files=forward.files
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
