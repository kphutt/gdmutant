"""Per-file test selection, Godot-free (docs/decisions/0017, step 3).

The pure rules in `engine.coverage` that step 3 adds, then the loop's two marker passes and its
selected mutant runs, driven by a fake framework that can open and close test-file windows. Real
Godot runs of the same thing live in `tests/test_selftest_live.py`.

The fake is deliberately able to *lie*: a map that credits the wrong test file, a suite whose files
only pass in one order, a set of files that fails on the unmutated source. Those are the cases the
whole step exists to survive, and a fake that could only tell the truth would prove nothing about
them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.engine.coverage import (
    RUN_EVERYTHING,
    CoverageAnalysis,
    CoverageMap,
    Hits,
    HitsUnreadable,
    MarkedCopy,
    build_map,
    read_hits,
    window_problems,
)
from gdmutant.engine.loop import (
    CoverageRunFailed,
    CoverageSelfCheckFailed,
    MutantOutcome,
    MutationRun,
    Verdict,
    _Trust,
    run,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.report import console_summary
from gdmutant.engine.runner import ReportedSuite, SuiteResult
from gdmutant.engine.spans import Span

A = "res://t/a.gd"
B = "res://t/b.gd"

# --- reading a hits file that carries windows ------------------------------------------------


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_windows_and_the_order_the_files_ran_are_read_back(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"hits": [0, 1, 2], "windows": {A: [0], B: [1], "": [2]}, "opened": [A, B]},
    )
    hits = read_hits(path)
    assert hits.spots == frozenset({0, 1, 2})
    assert hits.windows == {A: frozenset({0}), B: frozenset({1})}
    assert hits.load_time == frozenset({2})
    assert hits.opened == (A, B)


def test_a_file_that_runs_several_suites_is_one_file(tmp_path: Path) -> None:
    """GUT runs every inner class of a test script as a suite of its own, so one file opens a
    window several times in a row. It is still one file to hand back on a command line."""
    hits = read_hits(
        _write(tmp_path, {"hits": [0], "windows": {A: [0], B: []}, "opened": [A, A, B, A]})
    )
    assert hits.opened == (A, A, B, A)
    assert hits.files == (A, B)


def test_a_file_that_opened_a_window_and_reached_nothing_is_still_there(tmp_path: Path) -> None:
    """ "This file reaches nothing" and "the hook never fired" must stay tellable apart."""
    hits = read_hits(_write(tmp_path, {"hits": [0], "windows": {A: [0], B: []}, "opened": [A, B]}))
    assert hits.windows[B] == frozenset()
    assert hits.opened == (A, B)


def test_a_hits_file_with_no_windows_at_all_still_reads(tmp_path: Path) -> None:
    """``--coverage-analysis all`` writes no windows, and step 2 must keep working unchanged."""
    hits = read_hits(_write(tmp_path, {"hits": [4]}))
    assert hits.spots == frozenset({4})
    assert hits.windows == {}
    assert hits.opened == ()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"hits": [0], "windows": []}, "does not map test files to spot numbers"),
        ({"hits": [0], "windows": {"1": 2}}, "records a bad spot list"),
        ({"hits": [0], "windows": {"": ["x"]}}, "records a bad load-time spot list"),
        ({"hits": [0], "opened": "a"}, "does not list the test files that ran"),
        ({"hits": [0], "opened": [1]}, "does not list the test files that ran"),
    ],
)
def test_a_malformed_window_section_is_unreadable(
    tmp_path: Path, payload: object, message: str
) -> None:
    with pytest.raises(HitsUnreadable, match=message):
        read_hits(_write(tmp_path, payload))


def test_a_hits_file_that_is_not_an_object_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(HitsUnreadable, match="is not a JSON object"):
        read_hits(_write(tmp_path, [1, 2]))


def test_windows_and_opened_default_to_nothing_rather_than_to_no_value(tmp_path: Path) -> None:
    """`Hits` is read straight out of a file, so every field has to be a real value even when the
    file did not carry one. A default of ``None`` would come back as an attribute error deep inside
    `build_map` rather than as an empty map here."""
    bare = Hits(frozenset({1}))
    assert bare.windows == {}
    assert bare.load_time == frozenset()
    assert bare.opened == ()
    assert bare.files == ()


# The exact words, since a user reads them to decide what to fix, and the exact amount of a
# malformed file they quote back: enough to recognise it, not so much that it buries the sentence.


def test_the_malformed_window_messages_in_full(tmp_path: Path) -> None:
    path = _write(tmp_path, {"hits": [0], "windows": {"a": [1.5]}})
    with pytest.raises(HitsUnreadable) as caught:
        read_hits(path)
    assert str(caught.value) == (f"the hits file at {path} records a bad spot list for 'a': [1.5]")
    path = _write(tmp_path, {"hits": [0], "windows": {"": ["x"]}})
    with pytest.raises(HitsUnreadable) as load_time:
        read_hits(path)
    assert str(load_time.value) == (
        f"the hits file at {path} records a bad load-time spot list: ['x']"
    )


@pytest.mark.parametrize(
    ("payload", "what", "quoted"),
    [
        ({"hits": [0], "windows": "y" * 400}, "does not map test files to spot numbers", "y" * 400),
        ({"hits": [0], "opened": "z" * 400}, "does not list the test files that ran", "z" * 400),
    ],
)
def test_a_malformed_window_message_quotes_at_most_200_characters(
    tmp_path: Path, payload: dict[str, object], what: str, quoted: str
) -> None:
    path = _write(tmp_path, payload)
    with pytest.raises(HitsUnreadable) as caught:
        read_hits(path)
    assert str(caught.value) == f"the hits file at {path} {what}: {str(quoted)[:200]}"


def test_the_window_problem_messages_in_full() -> None:
    assert window_problems(Hits(frozenset({0})), _TWO_SUITES) == [
        "no test file opened a coverage window, so the runner's 'a test file started' hook never "
        "fired. Without it gdmutant cannot tell which test file reaches which line"
    ]


# --- the window rules -------------------------------------------------------------------------

_TWO_SUITES = SuiteResult(
    tests=4, failures=0, errors=0, suites=(ReportedSuite("a", 2), ReportedSuite("b", 2))
)


def test_a_pass_whose_windows_match_the_report_has_no_problems() -> None:
    assert window_problems(Hits(frozenset({0}), opened=(A, B)), _TWO_SUITES) == []


def test_a_hook_that_never_fired_is_a_problem() -> None:
    (problem,) = window_problems(Hits(frozenset({0})), _TWO_SUITES)
    assert "no test file opened a coverage window" in problem


def test_a_test_file_that_ran_without_opening_a_window_is_a_problem() -> None:
    (problem,) = window_problems(Hits(frozenset({0}), opened=(A,)), _TWO_SUITES)
    assert problem == (
        "1 test files opened a coverage window, but the run's own report describes 2. A test file "
        "that runs without opening a window is credited to no test, so a mutant on a line only it "
        "reaches would be run against the wrong tests"
    )


def test_more_windows_than_the_report_names_is_a_problem_too() -> None:
    assert window_problems(Hits(frozenset({0}), opened=(A, B, "c")), _TWO_SUITES)


# --- building the map from two passes -----------------------------------------------------------


def _hits(windows: Mapping[str, set[int]], load_time: set[int] | None = None) -> Hits:
    spots = {spot for spots in windows.values() for spot in spots} | (load_time or set())
    return Hits(
        spots=frozenset(spots),
        windows={name: frozenset(s) for name, s in windows.items()},
        load_time=frozenset(load_time or set()),
        opened=tuple(windows),
    )


def test_a_spot_both_passes_credit_to_the_same_files_is_selectable() -> None:
    built = build_map(_hits({A: {0}, B: {1}}), _hits({B: {1}, A: {0}}))
    assert built.files == {0: frozenset({A}), 1: frozenset({B})}
    assert built.order_dependent == frozenset()
    assert built.test_files == (A, B)


def test_a_spot_reached_with_no_test_file_running_runs_everything() -> None:
    built = build_map(_hits({A: {0}}, load_time={1}), _hits({A: {0, 1}}))
    assert built.files[1] is RUN_EVERYTHING
    # Load-time code is not order dependence: it belongs to no test in either pass.
    assert built.order_dependent == frozenset()


def test_a_load_time_spot_does_not_stop_the_map_at_the_spots_after_it() -> None:
    """Every spot either pass recorded gets an entry. A spot left out of the map reads as one no
    test reaches, which is the one answer that can turn a kill into a survivor."""
    built = build_map(_hits({A: {1, 2}}, load_time={0}), _hits({A: {1, 2}}, load_time={0}))
    assert set(built.files) == {0, 1, 2}
    assert built.files[1] == frozenset({A})


def test_load_time_in_the_reverse_pass_alone_is_enough() -> None:
    built = build_map(_hits({A: {1}}), _hits({A: {1}}, load_time={1}))
    assert built.files[1] is RUN_EVERYTHING


def test_a_spot_the_two_passes_credit_differently_runs_everything() -> None:
    """Deferred work that fires after its own test file ended lands in a different file when the
    files run backwards, which is exactly what this catches."""
    built = build_map(_hits({A: {0}, B: {0}}), _hits({B: {0}, A: set()}))
    assert built.files[0] is RUN_EVERYTHING
    assert built.order_dependent == frozenset({0})


def test_a_spot_only_one_pass_reached_runs_everything() -> None:
    built = build_map(_hits({A: {0}}), _hits({A: set()}))
    assert built.files[0] is RUN_EVERYTHING
    assert built.order_dependent == frozenset({0})


def test_a_spot_neither_pass_reached_is_absent_from_the_map() -> None:
    built = build_map(_hits({A: {0}}), _hits({A: {0}}))
    assert 7 not in built.files
    assert built.select(7) is RUN_EVERYTHING


def test_a_mutant_with_no_marker_runs_everything() -> None:
    assert build_map(_hits({A: {0}}), _hits({A: {0}})).select(None) is RUN_EVERYTHING


def test_a_spot_credited_to_no_file_by_both_passes_still_runs_everything() -> None:
    """A recorder that contradicts itself must never produce "run no tests at all"."""
    contradictory = Hits(spots=frozenset({3}), windows={A: frozenset()}, opened=(A,))
    built = build_map(contradictory, contradictory)
    assert built.files[3] is RUN_EVERYTHING


# --- confirming a selected kill -----------------------------------------------------------------


@dataclass
class _Clean:
    """A runner whose `run_selected` answer is scripted per set of files, and that counts calls."""

    answers: dict[tuple[str, ...], SuiteResult | Exception]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        self.calls.append(tuple(files))
        answer = self.answers[tuple(files)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    # The rest of `FileSelecting`, never reached by these tests.
    def install_windows(self, project_dir: str, recorder_dir: str) -> None: ...  # pragma: no cover
    def run_markers_files(  # pragma: no cover
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult: ...


def test_a_set_that_passes_unmutated_is_confirmed_once_and_remembered() -> None:
    runner = _Clean({(A,): SuiteResult(2, 0, 0)})
    trust = _Trust(runner)  # type: ignore[arg-type]
    assert trust.confirms("p", (A,), 1.0)
    assert trust.confirms("p", (A,), 1.0)
    assert runner.calls == [(A,)]
    assert trust.coupled == ()


def test_a_set_that_fails_unmutated_is_not_confirmed() -> None:
    runner = _Clean({(A,): SuiteResult(2, 1, 0)})
    trust = _Trust(runner)  # type: ignore[arg-type]
    assert not trust.confirms("p", (A,), 1.0)
    assert trust.coupled == ((A,),)


def test_a_set_that_runs_no_tests_at_all_is_not_confirmed() -> None:
    trust = _Trust(_Clean({(A,): SuiteResult(0, 0, 0)}))  # type: ignore[arg-type]
    assert not trust.confirms("p", (A,), 1.0)


def test_a_confirmation_that_cannot_run_is_not_a_confirmation() -> None:
    """The way out of an unconfirmed set is the whole suite, which is always sound."""
    trust = _Trust(_Clean({(A,): RuntimeError("Godot fell over")}))  # type: ignore[arg-type]
    assert not trust.confirms("p", (A,), 1.0)


# --- the loop, driven by a fake framework ---------------------------------------------------------

_SOURCE = """extends RefCounted


func a(x: int) -> bool:
\treturn x > 1


func b(y: int) -> bool:
\treturn y < 2
"""
#: The six mutants, in order: line 5 `>`->`>=`, `1`->`2`, `1`->`0`, then line 9 `<`->`<=`,
#: `2`->`3`, `2`->`1`. Spot 0 is line 5 and spot 1 is line 9.
_KILL_5 = "x >= 1"
_KILL_9 = "y <= 2"


@dataclass
class Lab:
    """A fake marker, test framework and runner in one, with per-test-file windows.

    `forward` and `reverse` are what the recorder writes in each pass: a test file mapped to the
    spots reached while it ran. `load_time` is what each pass records with no file running.
    `killers` says which test file catches which mutated text, which is the truth the map is
    measured against: point a window at the wrong file and the map is wrong in exactly the way the
    self-check exists to catch. `dirty` is the sets of files that fail even on the unmutated
    source, which is what an order-coupled suite does.
    """

    forward: dict[str, set[int]] = field(default_factory=lambda: {A: {0}, B: {1}})
    reverse: dict[str, set[int]] | None = None
    load_time: set[int] = field(default_factory=set)
    killers: dict[str, str] = field(default_factory=lambda: {_KILL_5: A, _KILL_9: B})
    dirty: set[tuple[str, ...]] = field(default_factory=set)
    reverse_failures: int = 0
    reverse_suites: tuple[ReportedSuite, ...] | None = None
    reverse_missing: set[str] = field(default_factory=set)
    spots: dict[int, int] = field(default_factory=lambda: {5: 0, 9: 1})
    baseline_tests: int = 4
    target: Path | None = None
    hits_path: Path | None = None
    windows_installed: list[str] = field(default_factory=list)
    marker_files: list[tuple[str, ...]] = field(default_factory=list)
    selections: list[tuple[str, ...] | None] = field(default_factory=list)
    started: bool = False

    # -- the marker half
    def mark(self, copy_dir: str, files: Mapping[str, tuple[str, Sequence[Mutant]]]) -> MarkedCopy:
        recorder = Path(copy_dir) / "_rec"
        recorder.mkdir()
        self.hits_path = recorder / "hits.json"
        return MarkedCopy(
            hits_path=str(self.hits_path),
            placements={
                rel: tuple(self.spots.get(m.span.line) for m in mutants)
                for rel, (_, mutants) in files.items()
            },
            recorder_dir="_rec",
        )

    def install_windows(self, project_dir: str, recorder_dir: str) -> None:
        self.windows_installed.append(recorder_dir)

    def _record(self, windows: dict[str, set[int]]) -> None:
        assert self.hits_path is not None
        spots = {s for group in windows.values() for s in group} | self.load_time
        self.hits_path.write_text(
            json.dumps(
                {
                    "hits": sorted(spots),
                    "windows": {
                        **{name: sorted(group) for name, group in windows.items()},
                        "": sorted(self.load_time),
                    },
                    "opened": list(windows),
                }
            ),
            encoding="utf-8",
        )

    def _suites(self) -> tuple[ReportedSuite, ...]:
        return tuple(ReportedSuite(name, 2) for name in self.forward)

    def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self._record(self.forward)
        return SuiteResult(self.baseline_tests, 0, 0, suites=self._suites())

    def run_markers_files(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        self.marker_files.append(tuple(files))
        # A real framework runs the files in the order it is given them, so the windows open in
        # that order too. `reverse_missing` is how a test makes one file's window go missing.
        windows = self.reverse if self.reverse is not None else self.forward
        self._record(
            {name: windows.get(name, set()) for name in files if name not in self.reverse_missing}
        )
        suites = self.reverse_suites if self.reverse_suites is not None else self._suites()
        return SuiteResult(self.baseline_tests, self.reverse_failures, 0, suites=suites)

    # -- the runner half
    def _verdict(self, project_dir: str, ran: Sequence[str]) -> SuiteResult:
        # The file inside the directory the runner was pointed at, so a ``--jobs`` worker's own
        # copy is what decides its own mutants, exactly as a real runner would see it.
        text = (Path(project_dir) / "t.gd").read_text(encoding="utf-8")
        failures = sum(1 for kill, where in self.killers.items() if kill in text and where in ran)
        if tuple(ran) in self.dirty:
            failures += 1
        return SuiteResult(2 * len(ran), failures, 0)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        if not self.started:
            self.started = True
            return SuiteResult(self.baseline_tests, 0, 0, suites=self._suites())
        self.selections.append(None)
        return self._verdict(project_dir, list(self.forward))

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        self.selections.append(tuple(files))
        return self._verdict(project_dir, files)


def _run(lab: Lab, tmp_path: Path, **kwargs: object) -> MutationRun:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    target = project / "t.gd"
    target.write_text(_SOURCE, encoding="utf-8")
    lab.target = target
    return run(
        str(project),
        str(target),
        _SOURCE,
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.PER_FILE,
        marker=lab,
        **kwargs,  # type: ignore[arg-type]
    )


def _verdicts(result: MutationRun) -> list[Verdict]:
    return [o.verdict for o in result.outcomes]


def test_each_mutant_runs_only_the_test_files_that_reach_it(tmp_path: Path) -> None:
    lab = Lab()
    result = _run(lab, tmp_path, self_check=0)
    assert _verdicts(result)[:1] == [Verdict.KILLED]
    assert _verdicts(result)[3:4] == [Verdict.KILLED]
    # Three line-5 mutants against a.gd and three line-9 ones against b.gd, plus one confirmation
    # run of each set, the first time that set produced a kill.
    assert lab.selections.count((A,)) == 4
    assert lab.selections.count((B,)) == 4
    assert result.selected >= 5
    assert result.test_files == 2


def test_the_window_hook_is_installed_into_the_marked_copy(tmp_path: Path) -> None:
    lab = Lab()
    _run(lab, tmp_path, self_check=0)
    assert lab.windows_installed == ["_rec"]


def test_the_second_pass_runs_the_same_files_backwards(tmp_path: Path) -> None:
    lab = Lab()
    _run(lab, tmp_path, self_check=0)
    assert lab.marker_files == [(B, A)]


def test_a_spot_no_pass_reached_is_still_no_coverage(tmp_path: Path) -> None:
    lab = Lab(forward={A: {0}, B: set()})
    result = _run(lab, tmp_path, self_check=0)
    assert _verdicts(result)[3:] == [Verdict.NO_COVERAGE] * 3
    assert result.no_coverage == 3


def test_a_load_time_spot_runs_every_test_file(tmp_path: Path) -> None:
    lab = Lab(forward={A: {0}, B: set()}, load_time={1})
    result = _run(lab, tmp_path, self_check=0)
    assert Verdict.NO_COVERAGE not in _verdicts(result)
    # The three line-9 mutants ran the whole suite, and so did the one the self-check sampled,
    # which a size of zero still keeps.
    assert lab.selections.count(None) == 4
    assert result.order_dependent == 0


def test_a_spot_the_passes_disagree_about_runs_every_test_file(tmp_path: Path) -> None:
    lab = Lab(forward={A: {0}, B: {0, 1}}, reverse={B: {0, 1}, A: set()})
    result = _run(lab, tmp_path, self_check=0)
    assert lab.selections.count(None) >= 3  # the three line-5 mutants
    assert result.order_dependent == 1


def test_a_kill_from_a_set_that_fails_unmutated_is_not_reported_as_a_kill_by_that_set(
    tmp_path: Path,
) -> None:
    """The suite went red and the mutant was in the tree, which is not the same as the mutant
    having done it."""
    lab = Lab(dirty={(A,)})
    result = _run(lab, tmp_path, self_check=0)
    # a.gd fails on its own whatever the source says, so all three line-5 mutants look killed by
    # it, and none of those kills is believed.
    assert result.order_coupled == 3
    coupled = [o for o in result.outcomes if o.order_coupled]
    assert all(o.selected is None for o in coupled)  # the whole suite decided in the end
    assert [o.verdict for o in coupled] == [Verdict.KILLED, Verdict.SURVIVED, Verdict.SURVIVED]


def test_a_set_is_confirmed_once_however_many_mutants_it_kills(tmp_path: Path) -> None:
    lab = Lab(killers={_KILL_5: A, "x > 2": A, _KILL_9: B})
    _run(lab, tmp_path, self_check=0)
    # Three line-5 mutants run against a.gd, two of them killed, and a.gd is confirmed once
    # between those two rather than once each.
    assert lab.selections.count((A,)) == 3 + 1


def test_the_reverse_pass_failing_refuses_selection_and_keeps_no_coverage(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    lab = Lab(forward={A: {0}, B: set()}, reverse_failures=2)
    result = _run(lab, tmp_path, self_check=0, progress=lines.append)
    assert result.no_coverage == 3  # the forward pass alone decides these
    assert result.selected == 0
    assert result.test_files == 0
    assert any("depends on the order its files run in" in line for line in lines)


def test_the_files_that_failed_in_reverse_are_named(tmp_path: Path) -> None:
    lines: list[str] = []
    lab = Lab(
        reverse_failures=1,
        reverse_suites=(ReportedSuite(B, 2, failures=1), ReportedSuite(A, 2)),
    )
    _run(lab, tmp_path, self_check=0, progress=lines.append)
    assert any(f"({B})" in line for line in lines)


class _Stubborn(Lab):
    """A runner that runs its own idea of the suite whatever file list it is handed."""

    def run_markers_files(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        return super().run_markers_files(project_dir, list(self.forward), timeout)


def _stubborn() -> Lab:
    return _Stubborn()


def test_a_runner_that_ignores_the_file_list_refuses_selection(tmp_path: Path) -> None:
    """Such a runner would run everything for every mutant while the summary claimed a saving. The
    reverse pass is the one place that can be seen, because it is the one pass whose answer is
    known in advance."""
    lines: list[str] = []
    result = _run(_stubborn(), tmp_path, self_check=0, progress=lines.append)
    assert result.selected == 0
    assert result.test_files == 0
    assert any("does not run only the test files it is given" in line for line in lines)
    assert any("configuration file" in line for line in lines)


def test_neither_refusal_needs_anyone_listening(tmp_path: Path) -> None:
    """A run with no progress callback still refuses selection, quietly and correctly."""
    for lab in (Lab(reverse_failures=1), _stubborn()):
        result = _run(lab, tmp_path / f"quiet-{id(lab)}", self_check=0)
        assert result.selected == 0
        assert result.test_files == 0


def test_the_progress_line_says_what_the_map_bought(tmp_path: Path) -> None:
    lines: list[str] = []
    _run(Lab(forward={A: {0}, B: set()}), tmp_path, self_check=0, progress=lines.append)
    assert any("run only the test files that reach them, out of 2" in line for line in lines)
    assert any("marked lines were reached differently in the two passes" in line for line in lines)


def test_a_reverse_pass_that_is_broken_some_other_way_still_stops_the_run(
    tmp_path: Path,
) -> None:
    lab = Lab(reverse_missing={B})  # one window fewer than its report names
    with pytest.raises(CoverageRunFailed) as caught:
        _run(lab, tmp_path, self_check=0)
    assert "not clean (reverse pass)" in str(caught.value)
    assert "1 test files opened a coverage window" in str(caught.value)


def test_a_forward_pass_whose_hook_never_fired_stops_the_run(tmp_path: Path) -> None:
    lab = Lab(forward={}, load_time={0, 1})
    with pytest.raises(CoverageRunFailed, match="no test file opened a coverage window"):
        _run(lab, tmp_path, self_check=0)


def test_a_map_that_credits_the_wrong_test_file_is_caught_by_the_self_check(
    tmp_path: Path,
) -> None:
    """The dangerous direction, built on purpose: the one test that can kill a mutant is not in
    the map, so a selected run reports it as survived while the whole suite kills it."""
    lab = Lab(forward={A: {1}, B: {0}})  # both spots credited to the wrong file
    with pytest.raises(CoverageSelfCheckFailed) as caught:
        _run(lab, tmp_path, self_check=None)
    assert "against the 1 test files the marker run said reach it gave 'survived'" in str(
        caught.value
    )
    assert "running it against the whole suite gave 'killed'" in str(caught.value)


def test_the_self_check_passes_when_the_map_is_right(tmp_path: Path) -> None:
    lab = Lab()
    result = _run(lab, tmp_path, self_check=None)
    assert result.selection_checked == 6
    assert result.no_coverage_checked == 0
    assert _verdicts(result) == [
        Verdict.KILLED,
        Verdict.SURVIVED,
        Verdict.SURVIVED,
        Verdict.KILLED,
        Verdict.SURVIVED,
        Verdict.SURVIVED,
    ]


def test_the_self_check_covers_both_kinds_at_once(tmp_path: Path) -> None:
    # b.gd reaches nothing, so it catches nothing either: the three line-9 mutants really are
    # unreached, and the whole suite must agree by letting them survive.
    lab = Lab(forward={A: {0}, B: set()}, killers={_KILL_5: A})
    result = _run(lab, tmp_path, self_check=None)
    assert result.no_coverage_checked == 3
    assert result.selection_checked == 3


def test_selection_survives_the_parallel_path_too(tmp_path: Path) -> None:
    lab = Lab()
    result = _run(lab, tmp_path, self_check=0, jobs=2)
    assert _verdicts(result)[0] is Verdict.KILLED
    assert result.selected >= 5


# --- what the summary says ----------------------------------------------------------------------


def _outcome(
    verdict: Verdict, selected: int | None = None, coupled: bool = False, checked: bool = False
) -> MutantOutcome:
    mutant = Mutant("f.gd", Span(1, 1, 1, 2), "numeric", "0", "1")
    return MutantOutcome(
        mutant, verdict, self_checked=checked, selected=selected, order_coupled=coupled
    )


def test_the_summary_reports_the_share_of_the_suite_each_mutant_ran() -> None:
    summary = console_summary(
        MutationRun(
            (_outcome(Verdict.KILLED, 2), _outcome(Verdict.SURVIVED, None)),
            coverage_analysis=True,
            test_files=10,
        )
    )
    assert "  selected: 1 of 2 mutants ran only the test files that reach them" in summary
    assert "(the suite has 10 test files)" in summary
    # One mutant ran 2 of 10 files and one ran all 10, so 60% of the suite per mutant.
    assert "  test files run: 60.0% of the suite per mutant, on average" in summary


def test_the_summary_names_one_test_file_in_the_singular() -> None:
    summary = console_summary(
        MutationRun((_outcome(Verdict.KILLED, 1),), coverage_analysis=True, test_files=1)
    )
    assert "(the suite has 1 test file)" in summary


def test_the_summary_counts_order_dependent_lines_and_order_coupled_kills() -> None:
    summary = console_summary(
        MutationRun(
            (_outcome(Verdict.KILLED, None, coupled=True),),
            coverage_analysis=True,
            test_files=4,
            order_dependent=2,
            order_coupled_sets=((A, B),),
        )
    )
    assert "  order-dependent lines: 2" in summary
    assert "  order-coupled kills: 1" in summary
    assert f"    these do not pass on their own: {A}, {B}" in summary


@pytest.mark.parametrize(("count", "tail"), [(4, "and 1 more such set"), (5, "and 2 more such")])
def test_the_summary_names_a_few_coupled_sets_and_counts_the_rest(count: int, tail: str) -> None:
    """A run where every set is coupled has one problem, not fifty, so the list is capped."""
    summary = console_summary(
        MutationRun(
            (_outcome(Verdict.KILLED, None, coupled=True),),
            coverage_analysis=True,
            test_files=9,
            order_coupled_sets=tuple((f"res://t/{n}.gd",) for n in range(count)),
        )
    )
    assert summary.count("these do not pass on their own") == 3
    assert tail in summary


def test_the_summary_says_nothing_about_selection_when_there_was_none() -> None:
    summary = console_summary(MutationRun((_outcome(Verdict.KILLED),), coverage_analysis=True))
    assert "selected:" not in summary
    assert "test files run:" not in summary


def test_the_share_is_unknown_when_no_mutant_ran() -> None:
    run_ = MutationRun((_outcome(Verdict.IGNORED),), coverage_analysis=True, test_files=3)
    assert run_.selected_share is None
    assert "test files run:" not in console_summary(run_)


def test_the_share_is_unknown_without_a_suite_to_be_a_share_of() -> None:
    assert MutationRun((_outcome(Verdict.KILLED, 2),)).selected_share is None


def test_the_summary_reports_both_halves_of_the_self_check() -> None:
    summary = console_summary(
        MutationRun(
            (
                _outcome(Verdict.NO_COVERAGE, checked=True),
                _outcome(Verdict.KILLED, 2, checked=True),
                _outcome(Verdict.SURVIVED, 2),
            ),
            coverage_analysis=True,
            test_files=5,
        )
    )
    assert "re-ran 1 of the 1 no-coverage mutants against the whole suite" in summary
    assert "re-ran 1 of the 2 mutants that ran only some test files" in summary


def test_an_empty_map_is_an_empty_map_and_says_so() -> None:
    summary = console_summary(MutationRun((_outcome(Verdict.KILLED),), coverage_analysis=True))
    assert "compared 0 mutants, because nothing was decided from the coverage map" in summary


def test_the_map_carries_the_suites_own_file_list() -> None:
    built = CoverageMap(files={}, order_dependent=frozenset(), test_files=(A, B))
    assert built.test_files == (A, B)
