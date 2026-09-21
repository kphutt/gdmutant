"""Coverage analysis, Godot-free (docs/decisions/0017, step 2).

The pure rules in `engine.coverage`, then the loop's marker run driven by a fake marker and a fake
runner. Real Godot runs of the same thing live in `tests/test_selftest_live.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.engine.coverage import (
    SELF_CHECK_SAMPLE,
    CoverageAnalysis,
    HitsUnreadable,
    MarkedCopy,
    _sample_key,
    clean_run_problems,
    read_hits,
    self_check_sample,
    uncovered,
)
from gdmutant.engine.loop import (
    BaselineFailed,
    CoverageRunFailed,
    CoverageSelfCheckFailed,
    MutantOutcome,
    MutationRun,
    SourceOutsideProject,
    Verdict,
    _progress_plan,
    _self_check,
    run,
    run_paths,
)
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.runner import MarkerRunnable, SuiteResult, SuiteTimeout
from gdmutant.engine.spans import Span

# --- the pure rules -------------------------------------------------------------------------------


def test_the_option_values_follow_strykers_names() -> None:
    assert [mode.value for mode in CoverageAnalysis] == ["off", "all", "per-file"]


def test_read_hits_returns_the_recorded_spots(tmp_path: Path) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": [3, 1, 3]}), encoding="utf-8")
    assert read_hits(path) == frozenset({1, 3})


def test_read_hits_accepts_an_empty_list_and_leaves_judging_it_to_the_clean_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hits.json"
    path.write_text('{"hits": []}', encoding="utf-8")
    assert read_hits(path) == frozenset()


def test_a_missing_hits_file_is_unreadable_never_nothing_reached(tmp_path: Path) -> None:
    with pytest.raises(HitsUnreadable, match="wrote no hits file") as caught:
        read_hits(tmp_path / "hits.json")
    assert "--path ." in str(caught.value)  # names the one cause a user can fix by themselves


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        '{"spots": [1]}',
        '{"hits": "1"}',
        '{"hits": [1, "2"]}',
        '{"hits": [1.5]}',
        '{"hits": [true]}',
    ],
)
def test_a_malformed_hits_file_is_unreadable(tmp_path: Path, text: str) -> None:
    path = tmp_path / "hits.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HitsUnreadable, match=r"hits file at .* (could not be read|is not a list)"):
        read_hits(path)


def test_an_unreadable_hits_path_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(HitsUnreadable, match="could not be read"):
        read_hits(tmp_path)  # a directory: an OSError other than "not found"


_CLEAN = SuiteResult(tests=4, failures=0, errors=0)


def test_a_clean_run_has_no_problems() -> None:
    assert clean_run_problems(_CLEAN, 4, frozenset({0})) == []


def test_a_failing_test_in_the_marker_run_is_a_problem() -> None:
    result = SuiteResult(tests=4, failures=1, errors=0, detail="test_x failed")
    (problem,) = clean_run_problems(result, 4, frozenset({0}))
    assert "not every test passed" in problem
    assert "1 failed, 0 errored" in problem
    assert problem.endswith("\ntest_x failed")


def test_an_erroring_test_in_the_marker_run_is_a_problem() -> None:
    (problem,) = clean_run_problems(SuiteResult(4, 0, 2), 4, frozenset({0}))
    assert "0 failed, 2 errored" in problem
    assert not problem.endswith("\n")


def test_a_script_error_in_the_marker_run_is_a_problem_even_when_every_test_passed() -> None:
    result = SuiteResult(tests=4, failures=0, errors=0, runtime_error="SCRIPT ERROR: boom")
    (problem,) = clean_run_problems(result, 4, frozenset({0}))
    assert "runtime error" in problem
    assert problem.endswith("\nSCRIPT ERROR: boom")


def test_a_test_count_that_differs_from_the_baseline_is_a_problem() -> None:
    (problem,) = clean_run_problems(SuiteResult(3, 0, 0), 4, frozenset({0}))
    assert problem == "the marker run ran 3 tests, but the baseline ran 4"


def test_more_tests_than_the_baseline_is_a_problem_too() -> None:
    assert clean_run_problems(SuiteResult(5, 0, 0), 4, frozenset({0}))


def test_zero_hits_is_a_problem() -> None:
    (problem,) = clean_run_problems(_CLEAN, 4, frozenset())
    assert "no marker recorded a single hit" in problem


def test_unreadable_hits_are_a_problem_with_their_own_message() -> None:
    assert clean_run_problems(_CLEAN, 4, HitsUnreadable("gone")) == ["gone"]


def test_every_problem_is_reported_not_just_the_first() -> None:
    result = SuiteResult(tests=2, failures=1, errors=0, runtime_error="SCRIPT ERROR")
    assert len(clean_run_problems(result, 4, frozenset())) == 4


def test_uncovered_is_the_mutants_whose_spot_was_never_hit() -> None:
    assert uncovered([0, 1, None, 1, 2], frozenset({0, 2})) == frozenset({1, 3})


def test_a_mutant_with_no_marker_is_never_uncovered() -> None:
    assert uncovered([None, None], frozenset()) == frozenset()


def _mutant(line: int, replacement: str = "x", path: str = "a.gd") -> Mutant:
    return Mutant(path, Span(line, 1, line, 2), "numeric", "0", replacement)


def test_the_self_check_sample_is_stable_and_capped() -> None:
    candidates = [("a.gd", i, _mutant(i + 1)) for i in range(10)]
    first = self_check_sample(candidates, 3)
    assert len(first) == 3
    assert self_check_sample(list(reversed(candidates)), 3) == first


def test_the_self_check_sample_does_not_depend_on_where_the_project_sits() -> None:
    here = [("a.gd", i, _mutant(i + 1, path="/one/a.gd")) for i in range(10)]
    there = [("a.gd", i, _mutant(i + 1, path="/two/a.gd")) for i in range(10)]
    assert self_check_sample(here, 2) == self_check_sample(there, 2)


def test_the_self_check_sample_is_not_simply_the_first_candidates() -> None:
    # A hash of something constant would keep the given order and pick the first every time.
    candidates = [("a.gd", i, _mutant(i + 1)) for i in range(40)]
    firsts = [self_check_sample(candidates[i : i + 10], 1) for i in range(0, 40, 10)]
    assert any(pick != frozenset({("a.gd", i * 10)}) for i, pick in enumerate(firsts))
    assert len(set(firsts)) == 4


def test_the_self_check_sample_keys_on_path_position_operator_and_replacement() -> None:
    base = _mutant(5)
    variants = [
        ("b.gd", base),
        ("a.gd", Mutant(base.path, Span(5, 3, 5, 4), "numeric", "0", "x")),
        ("a.gd", Mutant(base.path, Span(6, 1, 6, 2), "numeric", "0", "x")),
        ("a.gd", Mutant(base.path, base.span, "comparison", "0", "x")),
        ("a.gd", Mutant(base.path, base.span, "numeric", "0", "y")),
    ]
    keys = {_sample_key("a.gd", base), *(_sample_key(path, m) for path, m in variants)}
    assert len(keys) == 6


def test_the_self_check_takes_everything_when_asked() -> None:
    candidates = [("a.gd", i, _mutant(i + 1)) for i in range(7)]
    assert len(self_check_sample(candidates, None)) == 7


def test_the_self_check_always_takes_one_when_there_is_one() -> None:
    assert len(self_check_sample([("a.gd", 0, _mutant(1))], 0)) == 1
    assert self_check_sample([], 3) == frozenset()


def test_the_default_sample_is_a_few() -> None:
    assert SELF_CHECK_SAMPLE == 3


# --- the loop's marker run, driven by fakes -------------------------------------------------------

_SOURCE = """extends RefCounted


func a(x: int) -> bool:
\treturn x > 1


func b(y: int) -> bool:
\treturn y < 2
"""
# Mutants, in order: line 5 `>`->`>=`, `1`->`2`, `1`->`0`, then the same three shapes on line 9.
_LINE_5 = frozenset({0, 1, 2})
_LINE_9 = frozenset({3, 4, 5})


@dataclass
class Lab:
    """A fake marker and a fake runner in one, so the runner knows where the marker put the hits.

    The marker gives every mutant on a line in `spots` that spot id (lines missing from it get no
    marker, "run everything"). The marker run writes `hits` (or nothing, when it is ``None``) and
    returns `marker_result`. A mutant run kills any mutant that turns ``x > 1`` into ``x >= 1``."""

    spots: dict[int, int] = field(default_factory=lambda: {5: 0, 9: 1})
    hits: list[int] | None = field(default_factory=lambda: [0])
    marker_result: SuiteResult = field(default_factory=lambda: SuiteResult(3, 0, 0))
    kill: str = "x >= 1"
    baseline: SuiteResult = field(default_factory=lambda: SuiteResult(3, 0, 0))
    raise_on_mark: Exception | None = None
    raise_on_markers: Exception | None = None
    marked: list[dict[str, tuple[str, list[Mutant]]]] = field(default_factory=list)
    copies: list[Path] = field(default_factory=list)
    marker_timeouts: list[float | None] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    hits_path: Path | None = None
    target: Path | None = None

    def mark(self, copy_dir: str, files: Mapping[str, tuple[str, Sequence[Mutant]]]) -> MarkedCopy:
        if self.raise_on_mark is not None:
            raise self.raise_on_mark
        self.copies.append(Path(copy_dir))
        self.marked.append({k: (s, list(m)) for k, (s, m) in files.items()})
        self.hits_path = Path(copy_dir) / "hits.json"
        # A stale file, which the loop must delete before the run so it can never be read.
        self.hits_path.write_text('{"hits": [0, 1]}', encoding="utf-8")
        placements = {
            rel: tuple(self.spots.get(m.span.line) for m in mutants)
            for rel, (_, mutants) in files.items()
        }
        return MarkedCopy(hits_path=str(self.hits_path), placements=placements)

    def run_markers(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.marker_timeouts.append(timeout)
        assert self.hits_path is not None
        assert not self.hits_path.exists(), "the stale hits file was not removed"
        if self.raise_on_markers is not None:
            raise self.raise_on_markers
        if self.hits is not None:
            self.hits_path.write_text(json.dumps({"hits": self.hits}), encoding="utf-8")
        return self.marker_result

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        if not self.runs:
            self.runs.append("baseline")
            return self.baseline
        assert self.target is not None
        text = self.target.read_text(encoding="utf-8")
        self.runs.append(text)
        return SuiteResult(3, int(self.kill in text), 0)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    target = project / "t.gd"
    target.write_text(_SOURCE, encoding="utf-8")
    return project, target


def _run(lab: Lab, tmp_path: Path, **kwargs: object) -> MutationRun:
    project, target = _project(tmp_path)
    lab.target = target
    return run(
        str(project),
        str(target),
        _SOURCE,
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        **kwargs,  # type: ignore[arg-type]
    )


def _verdicts(result: MutationRun) -> list[Verdict]:
    return [o.verdict for o in result.outcomes]


def test_a_mutant_no_test_reaches_is_no_coverage_without_a_run(tmp_path: Path) -> None:
    lab = Lab()
    result = _run(lab, tmp_path, self_check=1)
    verdicts = _verdicts(result)
    assert verdicts[:3] == [Verdict.KILLED, Verdict.SURVIVED, Verdict.SURVIVED]
    assert verdicts[3:] == [Verdict.NO_COVERAGE] * 3
    # The baseline, the three reached mutants, and the one self-checked mutant. Nothing else ran.
    assert len(lab.runs) == 5
    assert sum(o.self_checked for o in result.outcomes) == 1
    assert result.coverage_analysis is True
    assert result.no_coverage == 3
    assert result.self_checked == 1


def test_every_reached_mutant_still_runs_the_whole_suite(tmp_path: Path) -> None:
    lab = Lab(hits=[0, 1])
    result = _run(lab, tmp_path)
    assert Verdict.NO_COVERAGE not in _verdicts(result)
    assert len(lab.runs) == 7


def test_a_mutant_with_no_marker_always_runs(tmp_path: Path) -> None:
    lab = Lab(spots={5: 0})  # line 9 cannot take a marker: run everything
    result = _run(lab, tmp_path)
    assert Verdict.NO_COVERAGE not in _verdicts(result)
    assert len(lab.runs) == 7


def test_the_score_does_not_move_when_coverage_analysis_is_on(tmp_path: Path) -> None:
    on = _run(Lab(), tmp_path / "on")
    lab = Lab()
    project, target = _project(tmp_path / "off")
    lab.target = target
    off = run(str(project), str(target), _SOURCE, lab, ADAPTER)
    assert on.mutation_score == off.mutation_score == 1 / 6
    assert off.coverage_analysis is False


def test_the_marker_run_gets_the_baseline_budget_not_a_mutants(tmp_path: Path) -> None:
    lab = Lab()
    _run(lab, tmp_path, timeout=42.0)
    assert lab.marker_timeouts == [None]  # the runner's own budget, as the baseline had


def test_the_copy_is_marked_by_project_relative_path_and_the_project_is_untouched(
    tmp_path: Path,
) -> None:
    lab = Lab()
    project, _ = _project(tmp_path / "x")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (project / "keep.txt").write_text("k", encoding="utf-8")
    lab.target = project / "t.gd"
    run(
        str(project),
        str(project / "t.gd"),
        _SOURCE,
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
    )
    (marked,) = lab.marked
    assert list(marked) == ["t.gd"]
    assert marked["t.gd"][0] == _SOURCE
    assert len(marked["t.gd"][1]) == 6
    (copy,) = lab.copies
    assert not copy.exists()  # the throwaway copy is gone
    assert (project / "t.gd").read_text(encoding="utf-8") == _SOURCE
    assert not (project / "hits.json").exists()


def test_the_copy_leaves_git_behind(tmp_path: Path) -> None:
    seen: dict[str, bool] = {}

    @dataclass
    class Peek(Lab):
        def mark(self, copy_dir: str, files):  # type: ignore[no-untyped-def]
            seen["git"] = (Path(copy_dir) / ".git").exists()
            seen["keep"] = (Path(copy_dir) / "keep.txt").exists()
            return super().mark(copy_dir, files)

    lab = Peek()
    project, target = _project(tmp_path)
    (project / ".git").mkdir()
    (project / "keep.txt").write_text("k", encoding="utf-8")
    lab.target = target
    run(str(project), str(target), _SOURCE, lab, ADAPTER, coverage=CoverageAnalysis.ALL, marker=lab)
    assert seen == {"git": False, "keep": True}


def _fails(lab: Lab, tmp_path: Path, match: str) -> str:
    with pytest.raises(CoverageRunFailed, match=match) as caught:
        _run(lab, tmp_path)
    message = str(caught.value)
    assert message.rstrip().endswith("(--coverage-analysis off) to run without it.")
    # Stopped before any mutant ran: only the baseline did.
    assert lab.runs == ["baseline"]
    return message


def test_a_missing_hits_file_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(hits=None), tmp_path, "wrote no hits file")


def test_zero_hits_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(hits=[]), tmp_path, "no marker recorded a single hit")


def test_a_script_error_in_the_marker_run_stops_the_run(tmp_path: Path) -> None:
    result = SuiteResult(3, 0, 0, runtime_error="SCRIPT ERROR: Invalid call")
    message = _fails(Lab(marker_result=result), tmp_path, "runtime error")
    assert "SCRIPT ERROR: Invalid call" in message


def test_a_failing_test_in_the_marker_run_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(marker_result=SuiteResult(3, 1, 0)), tmp_path, "not every test passed")


def test_a_test_count_mismatch_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(marker_result=SuiteResult(2, 0, 0)), tmp_path, "ran 2 tests, but the baseline ran 3")


def test_every_failed_rule_is_listed(tmp_path: Path) -> None:
    message = _fails(Lab(hits=[], marker_result=SuiteResult(2, 0, 0)), tmp_path, "was not clean")
    assert message.count("\n  - ") == 2


def test_a_marker_run_that_raises_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(raise_on_markers=RuntimeError("Godot crashed")), tmp_path, "Godot crashed")


def test_a_marker_run_that_hangs_stops_the_run(tmp_path: Path) -> None:
    _fails(Lab(raise_on_markers=SuiteTimeout("exceeded 9s")), tmp_path, "exceeded 9s")


def test_a_copy_that_cannot_be_marked_stops_the_run(tmp_path: Path) -> None:
    lab = Lab(raise_on_mark=RuntimeError("name taken"))
    _fails(lab, tmp_path, "could not prepare the marked copy for coverage analysis: name taken")


def test_a_coverage_failure_is_a_baseline_failure_so_every_caller_stops_on_it() -> None:
    assert issubclass(CoverageRunFailed, BaselineFailed)
    assert issubclass(CoverageSelfCheckFailed, CoverageRunFailed)


@dataclass
class PlainRunner:
    """A runner with no marker run at all."""

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        return SuiteResult(3, 0, 0)


def test_a_runner_that_cannot_run_markers_is_refused(tmp_path: Path) -> None:
    project, target = _project(tmp_path)
    assert not isinstance(PlainRunner(), MarkerRunnable)
    with pytest.raises(CoverageRunFailed, match="this run has one"):
        run(
            str(project),
            str(target),
            _SOURCE,
            PlainRunner(),
            ADAPTER,
            coverage=CoverageAnalysis.ALL,
            marker=Lab(),
        )


def test_no_marker_is_refused(tmp_path: Path) -> None:
    lab = Lab()
    project, target = _project(tmp_path)
    with pytest.raises(CoverageRunFailed, match="this run has one"):
        run(str(project), str(target), _SOURCE, lab, ADAPTER, coverage=CoverageAnalysis.ALL)


def test_neither_is_refused_by_name(tmp_path: Path) -> None:
    project, target = _project(tmp_path)
    with pytest.raises(CoverageRunFailed, match="this run has neither"):
        run(
            str(project),
            str(target),
            _SOURCE,
            PlainRunner(),
            ADAPTER,
            coverage=CoverageAnalysis.ALL,
        )


def test_per_file_is_refused_not_downgraded(tmp_path: Path) -> None:
    lab = Lab()
    project, target = _project(tmp_path)
    with pytest.raises(CoverageRunFailed, match="per-file is not built yet"):
        run(
            str(project),
            str(target),
            _SOURCE,
            lab,
            ADAPTER,
            coverage=CoverageAnalysis.PER_FILE,
            marker=lab,
        )
    assert lab.marked == []


def test_off_never_marks_anything(tmp_path: Path) -> None:
    lab = Lab()
    project, target = _project(tmp_path)
    lab.target = target
    result = run(
        str(project), str(target), _SOURCE, lab, ADAPTER, coverage=CoverageAnalysis.OFF, marker=lab
    )
    assert lab.marked == []
    assert result.coverage_analysis is False


def test_a_source_outside_the_project_is_refused_in_coverage_words(tmp_path: Path) -> None:
    lab = Lab()
    project, _ = _project(tmp_path)
    outside = tmp_path / "elsewhere.gd"
    outside.write_text(_SOURCE, encoding="utf-8")
    lab.target = outside
    with pytest.raises(SourceOutsideProject) as caught:
        run(
            str(project),
            str(outside),
            _SOURCE,
            lab,
            ADAPTER,
            coverage=CoverageAnalysis.ALL,
            marker=lab,
        )
    message = str(caught.value)
    assert "coverage analysis cannot mark it in a copy of the project" in message
    assert message.endswith("or turn coverage analysis off (--coverage-analysis off).")


def test_the_self_check_catches_a_map_that_hides_a_killable_mutant(tmp_path: Path) -> None:
    # The broken map: the spot on line 5 really is reached, and one of its mutants is killed, but
    # the marker run says nothing reached it. Checking every no-coverage mutant must catch that.
    lab = Lab(hits=[1])
    with pytest.raises(CoverageSelfCheckFailed) as caught:
        _run(lab, tmp_path, self_check=None)
    message = str(caught.value)
    assert "t.gd:5:11 (comparison: > -> >=)" in message
    assert "gave 'killed'" in message


def test_the_self_check_counts_what_it_compared(tmp_path: Path) -> None:
    result = _run(Lab(), tmp_path, self_check=None)
    assert result.self_checked == 3
    assert all(o.self_checked for o in result.outcomes[3:])


def test_the_self_check_is_off_when_nothing_is_uncovered(tmp_path: Path) -> None:
    result = _run(Lab(hits=[0, 1]), tmp_path)
    assert result.self_checked == 0
    assert result.coverage_analysis is True


def test_the_parallel_path_decides_exactly_as_the_serial_one(tmp_path: Path) -> None:
    serial = _run(Lab(), tmp_path / "s", self_check=1)

    @dataclass
    class Anywhere(Lab):
        """Kills by content wherever the worker's copy is, since --jobs mutates a copy."""

        def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            if not self.runs:
                self.runs.append("baseline")
                return self.baseline
            text = (Path(project_dir) / "t.gd").read_text(encoding="utf-8")
            self.runs.append(text)
            return SuiteResult(3, int(self.kill in text), 0)

    lab = Anywhere()
    parallel = _run(lab, tmp_path / "p", self_check=1, jobs=2)
    assert _verdicts(parallel) == _verdicts(serial)
    assert [o.self_checked for o in parallel.outcomes] == [o.self_checked for o in serial.outcomes]
    assert len(lab.runs) == 5


def test_an_uncovered_mutant_that_does_not_parse_stays_invalid(tmp_path: Path) -> None:
    # Validity comes first, so coverage analysis never moves a mutant out of `invalid`.
    @dataclass
    class Invalid:
        generate_mutants = staticmethod(ADAPTER.generate_mutants)

        @staticmethod
        def apply_mutant(mutant: Mutant, source: str) -> tuple[str, bool]:
            mutated, _ = ADAPTER.apply_mutant(mutant, source)
            return mutated, mutant.span.line != 9

    lab = Lab()
    project, target = _project(tmp_path)
    lab.target = target
    result = run(
        str(project),
        str(target),
        _SOURCE,
        lab,
        Invalid(),  # type: ignore[arg-type]
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        jobs=1,
    )
    assert _verdicts(result)[3:] == [Verdict.INVALID] * 3
    assert result.self_checked == 0


def test_the_parallel_path_keeps_an_uncovered_invalid_mutant_invalid(tmp_path: Path) -> None:
    @dataclass
    class Invalid:
        generate_mutants = staticmethod(ADAPTER.generate_mutants)

        @staticmethod
        def apply_mutant(mutant: Mutant, source: str) -> tuple[str, bool]:
            mutated, _ = ADAPTER.apply_mutant(mutant, source)
            return mutated, mutant.replacement != "3"

    lab = Lab()
    project, target = _project(tmp_path)
    lab.target = target
    result = run(
        str(project),
        str(target),
        _SOURCE,
        lab,
        Invalid(),  # type: ignore[arg-type]
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        jobs=2,
        self_check=1,
    )
    verdicts = _verdicts(result)
    assert verdicts[4] is Verdict.INVALID
    assert verdicts.count(Verdict.NO_COVERAGE) == 2


def test_run_paths_marks_every_file_in_one_marker_run(tmp_path: Path) -> None:
    lab = Lab(spots={5: 0, 9: 1})
    project, first = _project(tmp_path)
    second = project / "sub" / "u.gd"
    second.parent.mkdir()
    second.write_text(_SOURCE, encoding="utf-8")
    lab.target = first  # every mutant run reads the first file, so the second's all survive
    runs = run_paths(
        str(project),
        {str(first): _SOURCE, str(second): _SOURCE},
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        self_check=None,
    )
    (marked,) = lab.marked
    assert sorted(marked) == ["sub/u.gd", "t.gd"] or sorted(marked) == [
        str(Path("sub/u.gd")),
        "t.gd",
    ]
    assert lab.marker_timeouts and len(lab.marker_timeouts) == 1
    for file_run in runs.values():
        assert _verdicts(file_run)[3:] == [Verdict.NO_COVERAGE] * 3
        assert file_run.coverage_analysis is True
    assert sum(r.self_checked for r in runs.values()) == 6


def test_run_paths_with_coverage_off_is_unchanged(tmp_path: Path) -> None:
    lab = Lab()
    project, target = _project(tmp_path)
    lab.target = target
    runs = run_paths(str(project), {str(target): _SOURCE}, lab, ADAPTER, marker=lab)
    assert lab.marked == []
    assert Verdict.NO_COVERAGE not in _verdicts(runs[str(target)])


def test_the_marker_run_announces_itself_and_its_result(tmp_path: Path) -> None:
    lines: list[str] = []
    _run(Lab(), tmp_path, progress=lines.append, self_check=1)
    assert "running the suite once with coverage markers ..." in lines
    assert (
        "coverage: 3 of 6 mutants sit where no test reaches, so they need no run. The self-check "
        "runs 1 of them against the whole suite anyway."
    ) in lines
    assert "4 mutants to run (2 with no coverage)." in lines


def test_the_plan_line_counts_only_what_will_run() -> None:
    assert (
        _progress_plan(10, 12, 1, uncovered=3)
        == "7 mutants to run (2 ignored, 3 with no coverage)."
    )
    assert _progress_plan(4, 4, 1, uncovered=3) == "1 mutant to run (3 with no coverage)."
    assert _progress_plan(10, 10, 8, uncovered=7) == "3 mutants to run (7 with no coverage). " + (
        "Running up to 3 at a time."
    )


def _outcome(verdict: Verdict, line: int = 1) -> MutantOutcome:
    return MutantOutcome(_mutant(line), verdict)


def test_self_check_turns_a_survivor_into_confirmed_no_coverage() -> None:
    (checked,) = _self_check([_outcome(Verdict.SURVIVED)], frozenset({0}))
    assert checked.verdict is Verdict.NO_COVERAGE
    assert checked.self_checked is True


@pytest.mark.parametrize("verdict", [Verdict.INVALID, Verdict.IGNORED])
def test_self_check_leaves_a_mutant_that_never_ran_alone(verdict: Verdict) -> None:
    (checked,) = _self_check([_outcome(verdict)], frozenset({0}))
    assert checked == _outcome(verdict)


@pytest.mark.parametrize("verdict", [Verdict.KILLED, Verdict.TIMEOUT, Verdict.ERROR])
def test_self_check_fails_on_any_other_verdict(verdict: Verdict) -> None:
    with pytest.raises(CoverageSelfCheckFailed, match=f"gave '{verdict.value}'"):
        _self_check([_outcome(verdict)], frozenset({0}))


def test_self_check_touches_only_the_sample() -> None:
    outcomes = [_outcome(Verdict.KILLED, 1), _outcome(Verdict.SURVIVED, 2)]
    checked = _self_check(outcomes, frozenset({1}))
    assert checked[0] == outcomes[0]
    assert checked[1].verdict is Verdict.NO_COVERAGE


def test_the_score_counts_no_coverage_as_undetected() -> None:
    outcomes = (
        _outcome(Verdict.KILLED),
        _outcome(Verdict.SURVIVED),
        _outcome(Verdict.NO_COVERAGE),
        _outcome(Verdict.NO_COVERAGE),
    )
    result = MutationRun(outcomes)
    assert result.mutation_score == 0.25
    assert result.no_coverage == 2
    assert len(result.uncovered) == 2
    assert result.survivors == (outcomes[1].mutant,)


def test_a_run_of_only_no_coverage_scores_zero_not_none() -> None:
    assert MutationRun((_outcome(Verdict.NO_COVERAGE),)).mutation_score == 0.0


def test_the_heartbeat_counts_only_the_mutants_that_run(tmp_path: Path) -> None:
    lab = Lab()
    project, first = _project(tmp_path)
    second = project / "u.gd"
    second.write_text(_SOURCE, encoding="utf-8")
    lab.target = first
    lines: list[str] = []
    run_paths(
        str(project),
        {str(first): _SOURCE, str(second): _SOURCE},
        lab,
        ADAPTER,
        coverage=CoverageAnalysis.ALL,
        marker=lab,
        self_check=1,
        progress=lines.append,
    )
    # The first file ends on a forced heartbeat, whose count must reach its own total.
    beat = next(line for line in lines if " done in " in line)
    done, total = beat.split(" ")[1].split("/")
    assert done == total


# The exact words, since a user reads them to decide what to fix.


def test_the_missing_hits_message_in_full(tmp_path: Path) -> None:
    path = tmp_path / "hits.json"
    with pytest.raises(HitsUnreadable) as caught:
        read_hits(path)
    assert str(caught.value) == (
        f"the recorder wrote no hits file at {path}. It writes one when the suite's Godot process "
        "exits normally, so the run crashed, was killed, or ran a different project directory "
        "than the marked copy it was started in (a --command that names the project by an "
        "absolute path instead of --path . does that)"
    )


def test_the_unreadable_hits_message_in_full(tmp_path: Path) -> None:
    with pytest.raises(HitsUnreadable) as caught:
        read_hits(tmp_path)
    assert str(caught.value).startswith(f"the hits file at {tmp_path} could not be read: ")


def test_the_malformed_hits_message_quotes_at_most_200_characters(tmp_path: Path) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"spots": "x" * 400}), encoding="utf-8")
    with pytest.raises(HitsUnreadable) as caught:
        read_hits(path)
    quoted = str({"spots": "x" * 400})[:200]
    assert str(caught.value) == (f"the hits file at {path} is not a list of spot numbers: {quoted}")


def test_the_clean_run_messages_in_full() -> None:
    result = SuiteResult(tests=2, failures=1, errors=0, runtime_error="SCRIPT ERROR: x")
    assert clean_run_problems(result, 4, frozenset()) == [
        "not every test passed in the marker run (1 failed, 0 errored), though the same suite "
        "passed without markers",
        "the marker run's output holds a runtime error, which aborts the function it happens in "
        "and can make code a test reaches look unreached:\nSCRIPT ERROR: x",
        "the marker run ran 2 tests, but the baseline ran 4",
        "no marker recorded a single hit, so the recorder never ran. A suite that passed must "
        "reach some of the code it tests",
    ]


def test_the_self_check_key_is_path_line_column_operator_replacement() -> None:
    import hashlib

    mutant = _mutant(7)
    expected = hashlib.sha256(b"a.gd:7:1:numeric:x").hexdigest()
    assert _sample_key("a.gd", mutant) == expected
