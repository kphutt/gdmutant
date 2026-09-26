"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

import inspect
import os
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import MarkerRunner

from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.engine.adapter import Adapter
from gdmutant.engine.loop import (
    BaselineFailed,
    MutantPlan,
    ProgressStyle,
    SourceOutsideProject,
    SourceWriteFailed,
    TimeBudget,
    Verdict,
    _baseline_budget,
    _budget_note,
    _detect_eol,
    _evaluate,
    _Evaluation,
    _FileCoverage,
    _format_duration,
    _load_average_allows_more_workers,
    _plain_beat_every,
    _Progress,
    _progress_plan,
    _RunCoverage,
    _Trust,
    _wait_for_load_capacity,
    _write_source,
)
from gdmutant.engine.loop import run as _run
from gdmutant.engine.loop import run_paths as _run_paths
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import TableOperator
from gdmutant.engine.runner import ReportedSuite, SuiteResult, SuiteTimeout
from gdmutant.engine.spans import Span


# These tests drive the engine with fake runners against real GDScript, so they inject the real
# GDScript adapter once here (NF-3) rather than threading it through every call.
def run(*args, **kwargs):  # type: ignore[no-untyped-def]
    return _run(*args, adapter=ADAPTER, **kwargs)


def run_paths(*args, **kwargs):  # type: ignore[no-untyped-def]
    return _run_paths(*args, adapter=ADAPTER, **kwargs)


@dataclass
class RaiseAfterBaselineRunner:
    """Passes the baseline (first call), then raises — to test the ERROR verdict + restore."""

    calls: int = 0

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.calls += 1
        if self.calls == 1:
            return SuiteResult(tests=1, failures=0, errors=0)
        raise RuntimeError("runner boom")


@dataclass
class ScriptedRunner:
    """Returns or raises per call, from a fixed script — for exercising mid-run failures."""

    script: list[SuiteResult | Exception]
    calls: int = 0

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class ProjectDirRecordingRunner:
    """Records every project_dir it is handed (all-pass, so all mutants survive)."""

    seen: list[str] = field(default_factory=list)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.seen.append(project_dir)
        return SuiteResult(tests=1, failures=0, errors=0)


@dataclass
class TimeoutRecordingRunner:
    """Records the timeout handed to each run() call (baseline, then one per mutant); all-pass."""

    seen: list[float | None] = field(default_factory=list)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.seen.append(timeout)
        return SuiteResult(tests=1, failures=0, errors=0)


@dataclass
class PreparingRunner:
    """A `Preparable` runner over a fake clock: `prepare` and `run` each advance `clock` by a fixed
    cost, and every handed-in timeout is recorded — so a test can prove prepare's cost is excluded
    from the baseline wall-clock that derives per-mutant timeouts."""

    clock: list[float]
    prepare_cost: float
    suite_cost: float
    log: list[str] = field(default_factory=list)
    prepared_with: list[str] = field(default_factory=list)
    seen_timeouts: list[float | None] = field(default_factory=list)

    def prepare(self, project_dir: str) -> None:
        self.log.append("prepare")
        self.prepared_with.append(project_dir)
        self.clock[0] += self.prepare_cost

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.log.append("run")
        self.seen_timeouts.append(timeout)
        self.clock[0] += self.suite_cost
        return SuiteResult(tests=1, failures=0, errors=0)


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_prepare_runs_before_baseline_and_its_cost_is_excluded_from_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Preparable runner's one-time setup (e.g. a Godot import scan) must run BEFORE the baseline
    # clock starts, so its cost never inflates the derived per-mutant timeout or the ETA.
    # A slow prepare (5s) + a fast suite (0.05s): every mutant's timeout must be derived from the
    # 0.05s suite alone (→ the 10s floor), NOT 5.05s (→ 50.5s).
    from gdmutant.engine import loop as loop_mod

    clock = [0.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: clock[0])
    runner = PreparingRunner(clock=clock, prepare_cost=5.0, suite_cost=0.05)
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    messages: list[str] = []

    run(str(tmp_path), path, src, runner, progress=messages.append)

    assert runner.log[0] == "prepare"  # prepare precedes the first (baseline) run
    assert runner.log.count("prepare") == 1  # once, not per mutant
    assert runner.prepared_with == [str(tmp_path)]  # handed the real project dir, not None
    # The notice names the cost, not just the step: this is the Godot asset import, which on a
    # cold checkout runs for minutes with nothing on screen and gets read as a hang.
    assert "preparing the project (one-time; on a fresh checkout this can take minutes)" in messages
    # baseline_secs = suite_cost only (prepare excluded); mutant timeouts derive from it.
    mutant_timeouts = runner.seen_timeouts[1:]
    assert mutant_timeouts, "the source should produce at least one runnable mutant"
    # This runner's result names no test suites, so nothing says how much of the baseline was
    # tests and the whole wall-clock is multiplied, exactly as it was before the decomposition.
    assert all(t == TimeBudget(overhead=0.05).first() for t in mutant_timeouts)
    assert all(t != TimeBudget(overhead=5.05).first() for t in mutant_timeouts)  # prepare left out


def test_prepare_failure_becomes_baseline_failed(tmp_path: Path) -> None:
    # A runner that can't even prepare (e.g. Godot missing during the import scan) is a setup error,
    # surfaced as BaselineFailed — not a raw exception. Called with no progress callback, so this
    # also covers the prepare path when progress is None.
    @dataclass
    class FailPrepareRunner:
        def prepare(self, project_dir: str) -> None:
            raise RuntimeError("import boom")

        def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            return SuiteResult(tests=1, failures=0, errors=0)  # never reached

    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(BaselineFailed, match="could not prepare"):
        run(str(tmp_path), path, src, FailPrepareRunner())


def test_killed_survived_and_score(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    # The "test" only catches the mutation that yields ">=", i.e. the '>' -> '>=' comparison mutant.
    result = run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">="))

    assert (result.killed, result.survived, result.invalid) == (1, 2, 0)
    assert result.mutation_score == pytest.approx(1 / 3)
    (killed,) = [o.mutant for o in result.outcomes if o.verdict is Verdict.KILLED]
    assert (killed.original, killed.replacement) == (">", ">=")
    # Ordered, not a set: survivors are reported in the order mutants were generated (NF-1), which
    # the report's mutant ids and the console survivor list both depend on.
    assert [(m.original, m.replacement) for m in result.survivors] == [("and", "or"), ("<", "<=")]
    assert all(m.path == path for m in result.survivors)  # the real path flows to each mutant
    assert Path(path).read_text(encoding="utf-8") == src  # restored


def test_invalid_mutant_is_nf5_classified(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    bad = TableOperator("bad", {">": ("))",)})  # produces unparseable GDScript
    result = run(str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=(bad,))

    assert [o.verdict for o in result.outcomes] == [Verdict.INVALID]
    invalid = result.outcomes[0].mutant  # the outcome carries the real mutant, not a placeholder
    assert (invalid.original, invalid.replacement) == (">", "))")
    assert (result.killed, result.survived, result.invalid) == (0, 0, 1)
    assert result.mutation_score is None  # no killable mutants
    assert Path(path).read_text(encoding="utf-8") == src  # invalid short-circuits before any write


def test_invalid_mutant_does_not_stop_later_mutants(tmp_path: Path) -> None:
    # An invalid mutant must `continue` to the next mutant, not `break` the whole pass: the '>'
    # mutates to unparseable "))" (invalid), but the later '5' -> '6' mutant must still be run.
    src = "func f(a) -> bool:\n\treturn a > 5\n"
    path = _write(tmp_path, "f.gd", src)
    catalog = (TableOperator("x", {">": ("))",), "5": ("6",)}),)
    result = run(str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=catalog)
    assert [o.verdict for o in result.outcomes] == [Verdict.INVALID, Verdict.SURVIVED]
    assert {(o.mutant.original, o.mutant.replacement) for o in result.outcomes} == {
        (">", "))"),
        ("5", "6"),
    }


def test_runner_receives_the_real_project_dir_for_every_call(tmp_path: Path) -> None:
    # Both the baseline and each mutant run must be handed the real project_dir — never a
    # placeholder — so a mutant that passes None through is caught.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    runner = ProjectDirRecordingRunner()
    run(str(tmp_path), path, src, runner)
    assert len(runner.seen) >= 2  # baseline + at least one mutant
    assert all(seen == str(tmp_path) for seen in runner.seen)


def test_baseline_failure_raises(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    # The marker is present in the ORIGINAL source, so the unmutated baseline "fails". `$`-anchored
    # on the project-dir repr: with no runner detail the message ends cleanly at the quote (catches
    # a mutant that appends junk in the empty-detail branch).
    with pytest.raises(BaselineFailed, match=r"the unmutated test suite failed for '.+'$"):
        run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">"))
    assert Path(path).read_text(encoding="utf-8") == src


def test_baseline_failure_message_includes_suite_detail(tmp_path: Path) -> None:
    # When the baseline fails, any runner-supplied `detail` (e.g. a failing command's output) is
    # surfaced in the BaselineFailed message so a first run that can't go green is debuggable.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)

    @dataclass
    class DetailRunner:
        def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            return SuiteResult(tests=1, failures=1, errors=0, detail="harness said: boom")

    with pytest.raises(BaselineFailed, match=r"boom"):
        run(str(tmp_path), path, src, DetailRunner())


def test_a_zero_test_baseline_is_refused(tmp_path: Path) -> None:
    # SuiteResult(0, 0, 0).failed is False, so a baseline that ran NOTHING reads exactly like one
    # that passed — and then every mutant survives, producing a whole report of false survivors with
    # no error anywhere. The quietest way this tool can lie, and reachable from a typo in --tests.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    # Pinned the way its sibling `test_baseline_failure_raises` pins the red-baseline message: the
    # whole opening clause including the project-dir repr, not just the distinctive words. Matching
    # on "reported 0 tests" alone left the prefix untested, so a mutant that stopped naming *which*
    # project failed survived (caught by the line-scoped mutation run for this change).
    with pytest.raises(
        BaselineFailed, match=r"the unmutated \(baseline\) test suite for '.+' reported 0 tests"
    ):
        run(str(tmp_path), path, src, ScriptedRunner([SuiteResult(tests=0, failures=0, errors=0)]))
    assert Path(path).read_text(encoding="utf-8") == src


def test_the_zero_test_baseline_guard_is_language_neutral(tmp_path: Path) -> None:
    # It lives in the engine (NF-3), so its message must describe the CONDITION and never a
    # framework: naming GdUnit4/GUT/-gdir here would be a GDScript assumption in engine/, and the
    # check exists precisely because it must cover every runner, including ones not written yet.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(BaselineFailed) as excinfo:
        run(str(tmp_path), path, src, ScriptedRunner([SuiteResult(tests=0, failures=0, errors=0)]))
    message = str(excinfo.value)
    for framework_word in ("GdUnit4", "GUT", "-gdir", "godot", "Godot", "GDScript"):
        assert framework_word not in message, f"engine message names {framework_word}: {message}"


def test_a_zero_test_baseline_is_refused_by_run_paths(tmp_path: Path) -> None:
    # The multi-file entry point shares `_run_baseline`, so it must refuse identically — a guard
    # that covered only `run` would leave the directory path (the common CLI invocation) exposed.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(BaselineFailed, match="reported 0 tests"):
        run_paths(
            str(tmp_path), {path: src}, ScriptedRunner([SuiteResult(tests=0, failures=0, errors=0)])
        )


def test_a_zero_test_mutant_run_is_an_error_not_a_survivor(tmp_path: Path) -> None:
    # The mutant-side half of the same guard. The baseline proved this project collects tests, so a
    # later run collecting none did not "pass" — collection collapsed, most likely because the
    # mutant broke a file the suites load. SURVIVED there is a false survivor; ERROR is honest, and
    # is excluded from the score rather than inflating it.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"  # one mutant: '>'
    path = _write(tmp_path, "f.gd", src)
    runner = ScriptedRunner(
        [
            SuiteResult(tests=3, failures=0, errors=0),  # healthy baseline
            SuiteResult(tests=0, failures=0, errors=0),  # mutant: nothing collected
        ]
    )
    result = run(str(tmp_path), path, src, runner)
    assert [o.verdict for o in result.outcomes] == [Verdict.ERROR]
    assert (result.survived, result.errors) == (0, 1)
    assert result.mutation_score is None  # errors are excluded, never scored as detected


def test_a_zero_test_mutant_run_that_also_failed_stays_a_kill(tmp_path: Path) -> None:
    # The zero-test check must not steal a genuine detection: a run reporting no tests but a real
    # error/failure already surfaced the mutation, so it stays KILLED. Guards the check's `and not
    # result.failed` clause, which a naive `tests == 0` would drop.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    runner = ScriptedRunner(
        [
            SuiteResult(tests=3, failures=0, errors=0),  # healthy baseline
            SuiteResult(tests=0, failures=0, errors=2),  # mutant: the suite errored out
        ]
    )
    result = run(str(tmp_path), path, src, runner)
    assert [o.verdict for o in result.outcomes] == [Verdict.KILLED]


def test_baseline_runner_exception_becomes_baseline_failed(tmp_path: Path) -> None:
    # A runner that can't even run the unmutated suite (e.g. a missing godot binary) surfaces as
    # BaselineFailed, not a raw traceback.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(BaselineFailed, match=r"could not run the unmutated suite"):
        run(str(tmp_path), path, src, ScriptedRunner([RuntimeError("godot missing")]))


def test_runner_exception_is_tallied_as_error_and_file_restored(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b\n"  # one mutant: '>'
    path = _write(tmp_path, "f.gd", src)
    result = run(str(tmp_path), path, src, RaiseAfterBaselineRunner())
    assert [o.verdict for o in result.outcomes] == [Verdict.ERROR]
    assert result.errors == 1 and result.mutation_score is None
    assert Path(path).read_text(encoding="utf-8") == src  # restored despite the runner error


def test_suite_timeout_is_tallied_as_timeout_and_counts_as_detected(tmp_path: Path) -> None:
    # A mutation that hangs the suite (runner raises SuiteTimeout) is a DETECTION, not an error:
    # tallied TIMEOUT, distinct from ERROR, and counted toward the score like a kill.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"  # one mutant: '>'
    path = _write(tmp_path, "f.gd", src)
    ok = SuiteResult(tests=1, failures=0, errors=0)
    runner = ScriptedRunner([ok, SuiteTimeout("hung")])  # baseline ok; mutant hangs
    result = run(str(tmp_path), path, src, runner)
    assert [o.verdict for o in result.outcomes] == [Verdict.TIMEOUT]
    assert (result.timeouts, result.killed, result.errors) == (1, 0, 0)
    assert result.detected == 1
    assert result.mutation_score == 1.0  # timeout counts as detected → 1/1
    assert Path(path).read_text(encoding="utf-8") == src  # restored despite the timeout


def test_budget_multiplies_the_test_time_and_adds_the_measured_startup_back() -> None:
    # The whole formula, in one place: factor x the tests' own reported time, plus a constant,
    # plus the startup measured alongside them. The startup is added back UNMULTIPLIED, which is
    # the error the old `baseline x 10` made: a mutant cannot make a process boot slower, so
    # multiplying the boot inflated every budget by a number nobody chose.
    budget = TimeBudget(net=4.0, overhead=20.0, measured=True)
    assert budget.first() == 2.0 * 4.0 + 8.0 + 20.0  # 36.0, not 10 x 24 = 240
    assert budget.confirmation() == 10.0 * 4.0 + 8.0 + 20.0  # 68.0


def test_budget_is_floored_and_capped() -> None:
    assert TimeBudget(net=0.0, overhead=0.0, measured=True).first() == 10.0  # floor
    assert TimeBudget(net=0.01, overhead=0.5, measured=True).first() == 10.0  # 8.52 -> floored
    assert TimeBudget(net=1000.0, overhead=5.0, measured=True).first() == 600.0  # capped


def test_a_budget_on_the_floor_or_the_cap_gets_no_confirmation_run() -> None:
    # A confirmation that cannot allow more time than the first try already did would only repeat
    # it, so it is not run at all. That is the whole rule: a second run has to be able to say
    # something the first could not.
    assert TimeBudget(net=0.01, overhead=0.5, measured=True).confirmation() is None  # both floored
    assert TimeBudget(net=1000.0, overhead=5.0, measured=True).confirmation() is None  # both capped
    assert TimeBudget(net=4.0, overhead=20.0, measured=True).confirmation() == 68.0  # room to grow


def test_an_unmeasured_baseline_multiplies_the_whole_wall_clock() -> None:
    # The exit-code runner has no report, so nothing says which part of its baseline was tests.
    # Guessing zero would set the budget to the constant alone and turn slow suites into false
    # hangs, so the decomposition switches off and the pre-decomposition behaviour is kept.
    unmeasured = TimeBudget(net=0.0, overhead=24.0, measured=False)
    assert unmeasured.first() == 240.0
    assert unmeasured.confirmation() is None  # nothing to loosen, so nothing to confirm with


def test_an_explicit_timeout_overrides_everything_including_the_confirmation() -> None:
    # Someone who names a number meant that number, on every run and on every road to a verdict.
    fixed = TimeBudget(net=4.0, overhead=20.0, measured=True, fixed=3.0)
    assert fixed.first() == 3.0
    assert fixed.first(("a.gd",)) == 3.0
    assert fixed.confirmation() is None


def test_selected_files_are_budgeted_for_their_own_time() -> None:
    budget = TimeBudget(
        net=30.0, overhead=5.0, per_file={"a.gd": 1.0, "b.gd": 2.0, "c.gd": 27.0}, measured=True
    )
    assert budget.first(("a.gd", "b.gd")) == 2.0 * 3.0 + 8.0 + 5.0  # 19.0, not 2 x 30 + 13
    assert budget.first() == 2.0 * 30.0 + 8.0 + 5.0  # 73.0: no selection, so the whole suite


def test_a_mutant_with_no_selection_falls_back_to_the_whole_suite() -> None:
    # Every mutant unless --coverage-analysis per-file is on, and the ones it decided must run
    # everything anyway. `None` is the whole suite, and it must not be read as "no files, no time".
    budget = TimeBudget(net=30.0, overhead=5.0, per_file={"a.gd": 1.0}, measured=True)
    assert budget.first(None) == budget.first()
    assert budget.first(None) == 2.0 * 30.0 + 8.0 + 5.0


def test_an_unknown_selected_file_falls_back_to_the_whole_suite_not_to_zero() -> None:
    # A file the baseline's report never named has no known duration. Skipping it and adding up
    # the rest would hand the mutant a budget for part of its run while looking like a
    # measurement, which is the shape of a check that quietly measures less than it claims. One
    # unknown file is enough to fall back for the whole set.
    budget = TimeBudget(net=30.0, overhead=5.0, per_file={"a.gd": 1.0}, measured=True)
    assert budget.first(("a.gd", "mystery.gd")) == budget.first()
    assert budget.first(("mystery.gd",)) == budget.first()
    # And the fallback is genuinely bigger than the zero-seconds answer would have been.
    assert budget.first(("mystery.gd",)) > 2.0 * 0.0 + 8.0 + 5.0


def test_derived_timeout_is_handed_to_each_mutant_when_unset(tmp_path: Path) -> None:
    # No explicit timeout -> the loop derives one from the (instant) baseline and passes it to each
    # mutant run; the baseline itself is called with the runner's own budget (timeout=None).
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner)
    assert runner.seen[0] is None  # baseline uses the runner's configured budget
    assert runner.seen[1:] == [10.0, 10.0, 10.0]  # instant baseline -> derived floor of 10s


def test_explicit_timeout_overrides_derivation_for_each_mutant(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner, timeout=25.0)
    assert runner.seen[0] is None  # baseline still uses the runner's own budget
    assert runner.seen[1:] == [25.0, 25.0, 25.0]  # explicit value, not derived


def test_ignored_mutant_is_tallied_ignored_not_run_and_excluded_from_score(tmp_path: Path) -> None:
    # `ignore[comparison]` suppresses the two comparison mutants (`>`, `<`) — generated, NEVER run
    # (no suite call), tallied IGNORED, excluded from the score. The `and` mutant still runs and
    # survives (all-pass runner). Typed sole return, so no statement-deletion mutant is generated.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b  # gdmutant: ignore[comparison]\n"
    path = _write(tmp_path, "f.gd", src)
    runner = ProjectDirRecordingRunner()  # records one entry per actual run() call
    result = run(str(tmp_path), path, src, runner)

    assert {o.mutant.original: o.verdict for o in result.outcomes} == {
        ">": Verdict.IGNORED,
        "and": Verdict.SURVIVED,
        "<": Verdict.IGNORED,
    }
    assert (result.ignored, result.survived, result.killed) == (2, 1, 0)
    assert result.mutation_score == 0.0  # ignored excluded: 0 detected / (0 + 1 survived)
    assert len(runner.seen) == 2  # baseline + the `and` mutant only — the 2 ignored never ran
    assert Path(path).read_text(encoding="utf-8") == src


def test_runner_error_on_a_later_mutant_preserves_earlier_verdicts(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 mutants: >, and, <
    path = _write(tmp_path, "f.gd", src)
    ok = SuiteResult(tests=1, failures=0, errors=0)
    kill = SuiteResult(tests=1, failures=1, errors=0)
    # baseline ok; mutant 1 killed; mutant 2 raises mid-run; mutant 3 survived.
    runner = ScriptedRunner([ok, kill, RuntimeError("boom"), ok])
    result = run(str(tmp_path), path, src, runner)
    assert [o.verdict for o in result.outcomes] == [Verdict.KILLED, Verdict.ERROR, Verdict.SURVIVED]
    assert (result.killed, result.errors, result.survived) == (1, 1, 1)
    assert Path(path).read_text(encoding="utf-8") == src


def test_progress_has_no_per_mutant_line_only_plan_heartbeat_and_close(tmp_path: Path) -> None:
    # No "[i/N] path:line  a -> b  ... verdict" line per mutant anymore — on purpose. Progress is
    # just: the baseline notice, the pre-run plan, and the closing wall-clock. A single file's
    # forced end-of-file heartbeat is suppressed (`_mutate_file`'s `is_last_file`, default True): it
    # would restate the same numbers `finish` is about to print right after it. Source has 3
    # mutants: >, and, <. Explicit timeout keeps the run deterministic.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    lines: list[str] = []
    runner = MarkerRunner(target=path, kill_marker=">=")
    run(str(tmp_path), path, src, runner, timeout=10.0, progress=lines.append)
    assert lines[0] == "running the unmutated (baseline) suite ..."
    assert lines[1] == "3 mutants to run."
    assert lines[2].startswith("Done in ") and lines[2].endswith("3 mutants.")
    assert len(lines) == 3  # nothing per-mutant, no redundant close-of-file beat: plan, then close


def test_progress_counts_invalid_and_error_verdicts_without_naming_them(tmp_path: Path) -> None:
    # Every verdict still reaches the tally (checked via the closing count), but no per-mutant line
    # names which specific one was invalid or errored anymore — that detail now lives only in the
    # post-run report, not the live progress stream.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    invalid: list[str] = []
    bad = TableOperator("bad", {">": ("))",)})
    run(
        str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=(bad,), progress=invalid.append
    )
    # An invalid mutant never runs, so it is not a timing *sample* either: counting a mutant that
    # never reached the suite would make the closing wall-clock's "N mutants" a fiction.
    assert invalid[0] == "running the unmutated (baseline) suite ..."
    assert invalid[1].startswith("1 mutant to run.")
    assert invalid[-1].startswith("Done in ") and invalid[-1].endswith("0 mutants.")

    # An erroring mutant DID run (and DOES count as a sample), so it reaches the closing tally even
    # though nothing names it as the one that errored.
    errored: list[str] = []
    run(str(tmp_path), path, src, RaiseAfterBaselineRunner(), timeout=10.0, progress=errored.append)
    assert errored[0] == "running the unmutated (baseline) suite ..."
    assert errored[1].startswith("1 mutant to run.")
    assert errored[-1].startswith("Done in ") and errored[-1].endswith("1 mutant.")


def test_format_duration_scales_seconds_minutes_hours() -> None:
    assert _format_duration(0) == "0s"
    assert _format_duration(9) == "9s"
    assert _format_duration(59.4) == "59s"  # rounds to whole seconds
    assert _format_duration(60) == "1m 0s"
    assert _format_duration(135) == "2m 15s"  # the issue's own example
    assert _format_duration(3780) == "1h 3m"


def test_progress_plan_states_the_work_without_forecasting() -> None:
    # The pre-run line is facts only: what will run, nothing about how long it will take. It used to
    # also state the baseline wall-clock and the per-mutant timeout cap, as the fact that paced the
    # wait while nothing else did — now that the heartbeat itself fires every few seconds
    # (`_HEARTBEAT_SECS`), that job is live and repeated instead of a static number stated once.
    line = _progress_plan(runnable=18, total=18, jobs=1)
    assert line == "18 mutants to run."


def test_progress_plan_counts_ignored_separately() -> None:
    line = _progress_plan(runnable=18, total=21, jobs=1)
    assert line == "18 mutants to run (3 ignored)."


def test_progress_plan_names_the_worker_count() -> None:
    line = _progress_plan(runnable=18, total=18, jobs=4)
    assert line.endswith(" Running up to 4 at a time.")


def test_progress_plan_never_announces_more_workers_than_mutants() -> None:
    # The parallel path starts `min(jobs, mutants)` workers, so a small file under a large --jobs
    # (--jobs auto picks the CPU count) used to announce "Running 16 at a time" for 3 mutants.
    assert _progress_plan(runnable=3, total=3, jobs=16).endswith(" Running up to 3 at a time.")
    # One mutant runs one at a time, which the serial wording already says by saying nothing.
    assert "at a time" not in _progress_plan(runnable=1, total=4, jobs=16)


def test_progress_plan_is_singular_for_one_mutant() -> None:
    line = _progress_plan(runnable=1, total=1, jobs=1)
    assert line == "1 mutant to run."


def test_progress_plan_never_predicts_a_finish_time() -> None:
    # The whole point of the change. Nine surveyed mutation testers forecast an absolute duration
    # before the work starts; none of them do. Pin the absence so it cannot creep back.
    line = _progress_plan(runnable=99, total=99, jobs=1)
    for forecast in ("estimated", "≈", "at least", "left", "ETA"):
        assert forecast not in line


def test_the_worker_count_does_not_multiply_the_budget() -> None:
    # The budget takes no worker count at all, and that is the point. Multiplying it by W cancelled
    # the parallelism exactly where it was needed: N hanging mutants across W workers, each allowed
    # W x budget, cost the same wall-clock as running them one at a time. The contention allowance
    # lives in the constant instead, sized from measurement (docs/decisions/0020).
    budget = TimeBudget(net=4.0, overhead=20.0, measured=True)
    assert budget.first() == 36.0
    assert set(inspect.signature(budget.first).parameters) == {"files"}
    assert set(inspect.signature(budget.confirmation).parameters) == {"files"}


def _clock(style: ProgressStyle, total: int, lines: list[str]) -> _Progress:
    clock = _Progress(emit=lines.append, style=style)
    clock.begin_file(total)
    return clock


def test_heartbeat_reports_measured_progress_and_no_finish_time() -> None:
    # A rate extrapolation was built, measured against a real Godot project, and dropped: on a run
    # whose hanging mutants arrived late it read 3.2s at 25% done for a run that took 58.0s (95%
    # under). So the heartbeat states only what has already happened.
    lines: list[str] = []
    clock = _clock(ProgressStyle.RICH, total=18, lines=lines)
    for verdict in [Verdict.KILLED, Verdict.SURVIVED, Verdict.TIMEOUT]:
        clock.record(verdict, 1.0)
    clock.beat(force=True)
    assert lines[-1] == "… 3/18 done in 0s: 1 survived, 1 timed out."
    assert "left" not in lines[-1] and "~" not in lines[-1]


def test_heartbeat_waits_for_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # At most one line every _HEARTBEAT_SECS, so the heartbeat can never become the noise it exists
    # to prevent.
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.RICH, total=100, lines=lines)
    clock.record(Verdict.KILLED, 1.0)
    assert lines == []  # far too soon
    now[0] += loop_mod._HEARTBEAT_SECS
    clock.record(Verdict.KILLED, 1.0)
    assert lines == [f"… 2/100 done in {int(loop_mod._HEARTBEAT_SECS)}s: 0 survived, 0 timed out."]


def test_plain_style_needs_both_the_slower_clock_and_a_tenth_of_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-TTY / CI: 60s AND at least a tenth of the mutants. Requiring both makes the RARER rule
    # govern, which is what keeps a two-hour run from burying a build log.
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.PLAIN, total=100, lines=lines)
    now[0] += 600.0  # long past the 60s clock …
    clock.record(Verdict.KILLED, 1.0)
    assert lines == []  # … but only 1 of the 10 mutants that rule also wants
    for _ in range(9):
        clock.record(Verdict.KILLED, 1.0)
    assert lines == ["… 10/100 done in 10m 0s: 0 survived, 0 timed out."]


def test_plain_beat_every_is_a_tenth_of_the_file_but_never_zero() -> None:
    assert _plain_beat_every(100) == 10
    assert _plain_beat_every(18) == 2  # rounds up, so it can't stall
    assert _plain_beat_every(1) == 1
    assert _plain_beat_every(0) == 1  # never zero: that would beat on every mutant


def test_a_forced_heartbeat_always_closes_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # stryker-js#5929: a progress reporter that never shows the work reaching its end. Under PLAIN a
    # whole file can finish inside one interval and emit nothing at all, so the end of a file forces
    # a line — that is the only guarantee a log gets one.
    from gdmutant.engine import loop as loop_mod

    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: 1000.0)
    lines: list[str] = []
    clock = _clock(ProgressStyle.PLAIN, total=4, lines=lines)
    for _ in range(4):
        clock.record(Verdict.KILLED, 1.0)
    assert lines == []  # no interval elapsed
    clock.beat(force=True)
    assert lines == ["… 4/4 done in 0s: 0 survived, 0 timed out."]


def test_progress_style_none_silences_the_heartbeat_but_not_the_closing_line() -> None:
    lines: list[str] = []
    clock = _clock(ProgressStyle.NONE, total=2, lines=lines)
    clock.record(Verdict.KILLED, 1.0)
    clock.beat(force=True)
    assert lines == []
    clock.finish()
    assert lines[0].startswith("Done in ")  # a fact about the run, not progress chatter


def test_a_clock_with_no_emitter_does_nothing() -> None:
    clock = _Progress(emit=None, style=ProgressStyle.RICH)
    clock.begin_file(3)
    clock.record(Verdict.TIMEOUT, 5.0)
    clock.beat(force=True)
    clock.finish()  # must not raise
    assert clock.timeouts == 1


def test_closing_line_breaks_out_the_timeout_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    # The number every other test runner prints and this one did not — with the timeout cost split
    # out, because that is the cost nobody can see. On the measured run eight timeouts were four
    # minutes of six and a half, invisible before the run and after it.
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.NONE, total=18, lines=lines)
    for _ in range(10):
        clock.record(Verdict.KILLED, 14.0)
    for _ in range(8):
        clock.record(Verdict.TIMEOUT, 30.0)
    now[0] += 392.0
    clock.finish()
    assert lines == ["Done in 6m 32s. 18 mutants, 8 timed out (4m 0s of that)."]


def test_closing_line_adds_nothing_extra_when_nothing_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `timeout: 0` already lives in the per-verdict tally, so the zero case earns no clause here —
    # only a real, nonzero cost does, since that figure (the wall-clock those timeouts cost) exists
    # nowhere else.
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.NONE, total=1, lines=lines)
    clock.record(Verdict.KILLED, 2.0)
    now[0] += 25.0
    clock.finish()
    assert lines == ["Done in 25s. 1 mutant."]


def test_timeout_cost_is_measured_not_multiplied_out() -> None:
    # `timeouts × budget` would be wrong on the --jobs path, where the budget is scaled by the
    # worker count and the waits overlap. Only the real elapsed time is true on both paths.
    clock = _Progress(emit=None, style=ProgressStyle.NONE)
    clock.begin_file(2)
    clock.record(Verdict.TIMEOUT, 12.5)
    clock.record(Verdict.TIMEOUT, 7.5)
    assert clock.timeout_secs == 20.0


def test_a_run_ends_with_the_closing_wall_clock(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    lines: list[str] = []
    run(str(tmp_path), path, src, MarkerRunner(path, ">="), timeout=10.0, progress=lines.append)
    assert lines[1].startswith("1 mutant to run.")
    assert lines[-1].startswith("Done in ") and lines[-1].endswith("1 mutant.")


def test_a_multi_file_run_closes_once_for_the_whole_run(tmp_path: Path) -> None:
    # One wall-clock for the run, not one per file: "how long did that take" is a question about
    # the wait, and every file's mutants were part of the same wait.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    first = _write(tmp_path, "a.gd", src)
    second = _write(tmp_path, "b.gd", src)
    lines: list[str] = []
    run_paths(
        str(tmp_path),
        {first: src, second: src},
        MarkerRunner(first, ">="),
        timeout=10.0,
        progress=lines.append,
    )
    assert [line for line in lines if line.startswith("Done in ")] == [
        line for line in lines if line.startswith("Done in ")
    ][:1]
    assert sum(line.startswith("Done in ") for line in lines) == 1
    assert sum(line.startswith("1 mutant to run.") for line in lines) == 2  # one plan line per file


def test_only_a_non_last_file_gets_the_forced_end_of_file_beat(tmp_path: Path) -> None:
    # The first file's forced beat still fires (it closes out that file before "mutating <next> ..."
    # announces the next one), but the last file's would only restate what `finish` is about to say,
    # so `_mutate_file` skips it there.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    first = _write(tmp_path, "a.gd", src)
    second = _write(tmp_path, "b.gd", src)
    lines: list[str] = []
    run_paths(
        str(tmp_path),
        {first: src, second: src},
        MarkerRunner(first, ">="),
        timeout=10.0,
        progress=lines.append,
    )
    beats = [line for line in lines if line.startswith("… ")]
    assert len(beats) == 1
    assert beats[0].startswith("… 1/1 done in ")


def test_progress_defaults_to_silent(tmp_path: Path) -> None:
    # Omitting progress must run without error and produce the same outcomes — it is opt-in.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    result = run(str(tmp_path), path, src, MarkerRunner(path, ">="))
    assert [o.verdict for o in result.outcomes] == [Verdict.KILLED]


def test_run_is_deterministic(tmp_path: Path) -> None:
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    r1 = run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">="))
    r2 = run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">="))
    assert r1.outcomes == r2.outcomes


def test_run_paths_runs_baseline_once_then_mutates_each_file(tmp_path: Path) -> None:
    # Multi-file: the baseline suite runs ONCE, then each file's mutants run in turn. A
    # "mutating <path> ..." line marks each file; each file is restored after its own mutants.
    src_a = "func f(x) -> bool:\n\treturn x > 0\n"
    src_b = "func g(x) -> bool:\n\treturn x < 0\n"
    a = _write(tmp_path, "a.gd", src_a)
    b = _write(tmp_path, "b.gd", src_b)
    lines: list[str] = []
    runs = run_paths(
        str(tmp_path), {a: src_a, b: src_b}, ProjectDirRecordingRunner(), progress=lines.append
    )
    assert (
        lines.count("running the unmutated (baseline) suite ...") == 1
    )  # baseline once, not per file
    # POSIX-normalized, like the score lines and survivors printed with it. On Windows `a` is the
    # backslash form, so this also proves the host separator is gone. On POSIX the two are equal.
    posix_a, posix_b = Path(a).as_posix(), Path(b).as_posix()
    assert f"mutating {posix_a} ..." in lines and f"mutating {posix_b} ..." in lines
    if a != posix_a:
        assert f"mutating {a} ..." not in lines
    assert set(runs) == {a, b}  # one MutationRun per file, keyed by path
    assert runs[a].outcomes and all(
        o.verdict is Verdict.SURVIVED for o in runs[a].outcomes
    )  # all-pass
    assert Path(a).read_text(encoding="utf-8") == src_a  # restored
    assert Path(b).read_text(encoding="utf-8") == src_b


def test_run_paths_raises_baseline_failed_before_mutating_any_file(tmp_path: Path) -> None:
    # A red baseline aborts the whole multi-file pass (mutation-testing a red suite is meaningless),
    # and no file is left mutated. The marker is in the ORIGINAL source, so the baseline "fails".
    src = "func f(x) -> bool:\n\treturn x > 0\n"
    a = _write(tmp_path, "a.gd", src)
    with pytest.raises(BaselineFailed):
        run_paths(str(tmp_path), {a: src}, MarkerRunner(target=a, kill_marker=">"))
    assert Path(a).read_text(encoding="utf-8") == src


def test_run_paths_runs_silently_without_progress(tmp_path: Path) -> None:
    # `progress=None` (the default) must run without error — the "mutating <path>" line is opt-in.
    src = "func f(x) -> bool:\n\treturn x > 0\n"
    a = _write(tmp_path, "a.gd", src)
    runs = run_paths(str(tmp_path), {a: src}, ProjectDirRecordingRunner())
    assert set(runs) == {a} and runs[a].outcomes


# --- NF-3: the engine is language-neutral (the adapter is injected, never imported) ---------------


def test_no_engine_module_imports_a_language_adapter() -> None:
    # The whole point of the Adapter seam: importing the engine must never drag in a language
    # adapter. Statically assert that no `gdmutant/engine/*.py` imports `gdmutant.adapters`.
    import ast

    from gdmutant.engine import loop as _engine_loop

    engine_dir = Path(_engine_loop.__file__).parent
    offenders: list[str] = []
    for module_file in engine_dir.rglob("*.py"):
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "gdmutant.adapters"
            ):
                offenders.append(f"{module_file.name}: from {node.module}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{module_file.name}: import {n.name}"
                    for n in node.names
                    if n.name.startswith("gdmutant.adapters")
                ]
    assert not offenders, f"engine must not import an adapter (NF-3): {offenders}"


def test_run_drives_a_custom_non_gdscript_adapter(tmp_path: Path) -> None:
    # Behavioral proof of injection: the engine runs against a *fake* adapter (not gdscript). It
    # must call the injected `generate_mutants`, so the loop is genuinely adapter-agnostic.
    src = "func f(x) -> bool:\n\treturn x > 0\n"
    path = _write(tmp_path, "f.gd", src)
    seen: list[tuple[str, str]] = []

    def fake_generate(p: str, s: str, catalog: object) -> list[Mutant]:
        seen.append((p, s))
        return []  # no mutants → only the baseline runs; proves generate was reached

    def fake_apply(mutant: Mutant, s: str) -> tuple[str, bool]:
        raise AssertionError("apply_mutant must not run when there are no mutants")

    fake = Adapter(generate_mutants=fake_generate, apply_mutant=fake_apply)
    result = _run(
        str(tmp_path), path, src, MarkerRunner(target=path, kill_marker="ZZZ"), adapter=fake
    )
    assert seen == [(path, src)]  # the engine used the injected adapter, not gdscript
    assert result.outcomes == ()


@dataclass
class ProjectRelMarkerRunner:
    """Like MarkerRunner, but reads the target relative to the `project_dir` it is handed each call
    — so it reacts to whatever a parallel worker wrote to ITS OWN project copy, not a fixed path.
    This mirrors the real runners (CommandRunner/GdUnit4Runner both operate on the given dir)."""

    relname: str
    kill_marker: str
    tests: int = 3

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        content = (Path(project_dir) / self.relname).read_text(encoding="utf-8")
        return SuiteResult(tests=self.tests, failures=int(self.kill_marker in content), errors=0)


def _outcome_key(result: object) -> list[tuple[int, int, str, str, Verdict]]:
    # Order-preserving fingerprint of a run: (line, col, original, replacement, verdict) per mutant.
    return [
        (
            o.mutant.span.line,
            o.mutant.span.column,
            o.mutant.original,
            o.mutant.replacement,
            o.verdict,
        )
        for o in result.outcomes  # type: ignore[attr-defined]
    ]


def test_parallel_matches_serial_verdicts_and_order(tmp_path: Path) -> None:
    # The core correctness guarantee: --jobs is sound. A parallel run must produce byte-identical
    # verdicts to the serial oracle, in the same generation order (NF-1) — process isolation on
    # per-worker copies means concurrency changes only the wall-clock, never a verdict.
    src = (
        "func f(a, b) -> bool:\n"
        "\tvar hi = a > b\n"  # '>' -> '>=' killed by the ">=" marker
        # 'and' -> 'or' survives (no ">=" produced); the '<' comparison mutant is ignored.
        "\treturn hi and a < b  # gdmutant: ignore[comparison]\n"
    )
    serial_dir = tmp_path / "serial"
    serial_dir.mkdir()
    serial_path = _write(serial_dir, "f.gd", src)
    serial = run(str(serial_dir), serial_path, src, ProjectRelMarkerRunner("f.gd", ">="))

    parallel_dir = tmp_path / "parallel"
    parallel_dir.mkdir()
    parallel_path = _write(parallel_dir, "f.gd", src)
    lines: list[str] = []
    parallel = run(
        str(parallel_dir),
        parallel_path,
        src,
        ProjectRelMarkerRunner("f.gd", ">="),
        jobs=4,  # more jobs than mutants: exercises the min(jobs, total) worker cap
        progress=lines.append,
    )

    # This covers pass/fail/ignored/invalid verdicts + ordering for a deterministic runner; the
    # timeout axis (a budget scaled under contention) is pinned separately below — this fake runner
    # is instant and never times out.
    assert _outcome_key(parallel) == _outcome_key(serial)  # identical verdicts AND order
    verdicts = {v for *_, v in _outcome_key(serial)}
    assert verdicts == {
        Verdict.KILLED,
        Verdict.SURVIVED,
        Verdict.IGNORED,
    }  # a real mix ran parallel
    assert any(line.startswith("Done in ") for line in lines)  # parallel still reaches the close
    # The original project file is NEVER mutated in the parallel path — only the worker copies are.
    assert Path(parallel_path).read_text(encoding="utf-8") == src


def test_parallel_gives_every_worker_the_unscaled_budget(tmp_path: Path) -> None:
    # Soundness on the timeout axis, the other way round from how it used to be argued. The budget
    # used to be multiplied by the worker count, on the reasoning that W workers contend so each
    # suite runs ~Wx slower. Measured on a real project, eight concurrent Godot processes cost 28%,
    # not 700%, and the multiplier cancelled the parallelism on exactly the mutants that hang. The
    # contention allowance now lives in the budget's constant, and a mutant that still runs long is
    # re-run before it is called a hang (docs/decisions/0020).
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner, timeout=5.0, jobs=2)
    baseline, *mutant_timeouts = runner.seen
    assert baseline is None  # the baseline still uses the runner's own budget
    assert len(mutant_timeouts) == 3  # every mutant ran
    assert all(t == 5.0 for t in mutant_timeouts)  # 5.0, not 5.0 x min(jobs=2, mutants=3)


def test_load_average_allows_more_workers_with_no_getloadavg_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows has no os.getloadavg at all (not merely raise-y) — the platform gdmutant treats as a
    # real deployment target must still get a plain answer, not a crash.
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert _load_average_allows_more_workers(4.0) is True


def test_load_average_allows_more_workers_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0), raising=False)
    assert _load_average_allows_more_workers(4.0) is True


def test_load_average_blocks_more_workers_at_or_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getloadavg", lambda: (4.0, 4.0, 4.0), raising=False)
    assert _load_average_allows_more_workers(4.0) is False


def test_load_average_allows_more_workers_when_getloadavg_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A platform that HAS getloadavg but can't answer right now (OSError is documented) must fail
    # open, the same as no signal at all — never block a run over the tool's own uncertainty.
    def boom() -> tuple[float, float, float]:
        raise OSError("load average unavailable")

    monkeypatch.setattr(os, "getloadavg", boom, raising=False)
    assert _load_average_allows_more_workers(4.0) is True


def test_wait_for_load_capacity_returns_immediately_once_load_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gdmutant.engine import loop as loop_mod

    monkeypatch.setattr(loop_mod, "_load_average_allows_more_workers", lambda threshold: True)
    slept: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", slept.append)
    _wait_for_load_capacity(4.0)
    assert slept == []  # never polled — capacity was already there


def test_wait_for_load_capacity_gives_up_after_the_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Never blocks forever under sustained load (unlike `make -l`, which can defer a job
    # indefinitely) — "looks hung" is already the #1 documented reason people abandon a run.
    from gdmutant.engine import loop as loop_mod

    monkeypatch.setattr(loop_mod, "_load_average_allows_more_workers", lambda threshold: False)
    monkeypatch.setattr(loop_mod, "_LOAD_THROTTLE_MAX_WAIT_SECS", 0.05)
    monkeypatch.setattr(loop_mod, "_LOAD_THROTTLE_POLL_SECS", 0.01)
    started = time.monotonic()
    _wait_for_load_capacity(4.0)
    assert time.monotonic() - started < 2.0  # bounded, not indefinite


def test_jobs_auto_holds_off_every_worker_but_the_first_under_load(tmp_path: Path) -> None:
    # --jobs auto (jobs_auto=True) must check load before starting worker 1 and up, but never
    # before worker 0 — an auto run should never do LESS than a serial one would.
    from gdmutant.engine import loop as loop_mod

    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)
    calls: list[float] = []
    original = loop_mod._wait_for_load_capacity

    def counting(threshold: float) -> None:
        calls.append(threshold)
        original(threshold)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(loop_mod, "_wait_for_load_capacity", counting)
        m.setattr(loop_mod, "_load_average_allows_more_workers", lambda threshold: True)
        run(str(tmp_path), path, src, TimeoutRecordingRunner(), jobs=3, jobs_auto=True)
    # worker_count = min(jobs=3, mutants=3) = 3 workers, so 2 are checked (index 1 and 2), never 0.
    assert len(calls) == 2


def test_explicit_jobs_never_throttles_even_with_more_workers_than_cores(tmp_path: Path) -> None:
    # An explicit --jobs N (jobs_auto=False, the default) must behave exactly as every prior
    # release did: no load check at all, ever — only 'auto' opts in to throttling.
    from gdmutant.engine import loop as loop_mod

    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)
    calls: list[float] = []
    with pytest.MonkeyPatch.context() as m:
        m.setattr(loop_mod, "_wait_for_load_capacity", lambda threshold: calls.append(threshold))
        run(str(tmp_path), path, src, TimeoutRecordingRunner(), jobs=3)
    assert calls == []


def test_a_parallel_worker_is_given_the_same_budget_a_serial_run_would_give(
    tmp_path: Path,
) -> None:
    # The budget a `--jobs N` worker actually enforces is the serial one, unscaled. It used to be
    # multiplied by the worker count, which made N hanging mutants across N workers cost exactly
    # what running them one at a time would have (see `_run_mutants_parallel`). Checked against a
    # real parallel run rather than against the arithmetic alone, because the arithmetic living in
    # the right place proves nothing about which number reaches the runner.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)

    parallel = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, parallel, timeout=5.0, jobs=2)
    serial = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, serial, timeout=5.0)

    _, *under_jobs = parallel.seen  # drop the baseline, which uses the runner's own budget
    _, *alone = serial.seen
    assert under_jobs and all(t == 5.0 for t in under_jobs)
    assert under_jobs == alone


def test_parallel_classifies_an_invalid_mutant_like_serial(tmp_path: Path) -> None:
    # The INVALID (NF-5) branch must resolve identically under --jobs: a mutant that doesn't parse
    # is never run, in parallel just as in serial.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    bad = TableOperator("bad", {">": ("))",)})  # unparseable GDScript
    serial_dir = tmp_path / "serial"
    serial_dir.mkdir()
    sp = _write(serial_dir, "f.gd", src)
    serial = run(str(serial_dir), sp, src, ProjectRelMarkerRunner("f.gd", "ZZZ"), catalog=(bad,))
    par_dir = tmp_path / "parallel"
    par_dir.mkdir()
    pp = _write(par_dir, "f.gd", src)
    parallel = run(
        str(par_dir), pp, src, ProjectRelMarkerRunner("f.gd", "ZZZ"), catalog=(bad,), jobs=2
    )
    assert _outcome_key(parallel) == _outcome_key(serial)
    assert [v for *_, v in _outcome_key(parallel)] == [Verdict.INVALID]


def test_parallel_apply_error_propagates(tmp_path: Path) -> None:
    # Applying a mutant (gdtoolkit) runs single-threaded in the parallel path's serial pre-pass —
    # NOT thread-safe, so it must not run in workers. If it raises, that propagates straight out
    # (no worker has started yet), so a real adapter bug fails loud.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)

    def fake_generate(p: str, s: str, _catalog: object) -> tuple[Mutant, ...]:
        return (Mutant(p, Span(2, 9, 2, 10), "comparison", ">", ">="),)

    def fake_apply(_mutant: Mutant, _source: str) -> tuple[str, bool]:
        raise RuntimeError("apply boom")

    adapter = Adapter(generate_mutants=fake_generate, apply_mutant=fake_apply)
    with pytest.raises(RuntimeError, match="apply boom"):
        _run(
            str(tmp_path),
            path,
            src,
            ProjectRelMarkerRunner("f.gd", "ZZZ"),
            adapter=adapter,
            jobs=2,
        )


def test_parallel_worker_run_error_is_reraised_in_the_main_thread(tmp_path: Path) -> None:
    # A BaseException raised inside a worker's test run (which _run_one's `except Exception`
    # deliberately does NOT swallow) must be captured and re-raised on the main thread — never lost
    # in a dead worker, which would silently drop a mutant from the report.
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)

    class WorkerBoom(BaseException):
        pass

    @dataclass
    class BoomRunner:
        calls: int = 0

        def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
            self.calls += 1
            if self.calls == 1:
                return SuiteResult(tests=1, failures=0, errors=0)  # baseline passes
            raise WorkerBoom("worker boom")

    with pytest.raises(WorkerBoom, match="worker boom"):
        run(str(tmp_path), path, src, BoomRunner(), jobs=2)


def test_a_run_restores_the_file_byte_for_byte_including_its_line_endings(
    tmp_path: Path,
) -> None:
    # _run_one's finally-block promises "never leave the project mutated". That promise was only
    # true at the TEXT level: source arrives normalised to LF (read_text does that), and write_text
    # translates LF back to os.linesep -- so on Windows every run silently rewrote the target with
    # CRLF. Against a project declaring `eol=lf` in .gitattributes, that leaves each mutated file
    # permanently "modified" with an empty diff.
    #
    # Asserting on bytes rather than text is what makes this catch the bug at all.
    #
    # This test and its LF twin below are a PAIR, and neither is redundant: each catches the
    # regression on the platform where the other cannot. Verified by reverting the fix:
    #   - here (CRLF fixture): fails on Linux/macOS, where write_text would restore LF into a
    #     file that should be CRLF. On Windows the bug accidentally produces the right answer,
    #     so this one passes there.
    #   - the LF twin: fails on Windows, where write_text turns LF into CRLF.
    # CI runs Linux, so this is the one that guards the build; the twin guards the dev machine.
    # Deleting either leaves half the platforms unprotected.
    path = tmp_path / "crlf.gd"
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_bytes(src.replace("\n", "\r\n").encode("utf-8"))
    before = path.read_bytes()
    assert b"\r\n" in before  # fixture sanity: the file really is CRLF

    result = run(str(tmp_path), str(path), src, MarkerRunner(str(path), "a >= b"))

    assert result.outcomes, "expected the loop to have produced and run mutants"
    assert path.read_bytes() == before, "the run did not restore the file byte-for-byte"


def test_eol_detection_falls_back_to_lf_when_the_file_cannot_be_read() -> None:
    # _detect_eol samples the file before the first write. If that read fails -- the path is
    # gone, or unreadable -- it must not take the whole run down: the caller is about to write
    # the file anyway, and LF is the safe default. Covers the OSError arm, which the two
    # round-trip tests below never reach because they always have a readable file.
    assert _detect_eol(Path("no", "such", "file.gd")) == "\n"


def test_an_lf_file_stays_lf_after_a_run(tmp_path: Path) -> None:
    # The other direction: preserving CRLF must not mean introducing it. A project that is LF on
    # disk has to come back LF, whatever OS the run happened on.
    path = tmp_path / "lf.gd"
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_bytes(src.encode("utf-8"))
    before = path.read_bytes()

    run(str(tmp_path), str(path), src, MarkerRunner(str(path), "a >= b"))

    after = path.read_bytes()
    assert b"\r\n" not in after, "an LF file must not gain carriage returns"
    assert after == before


# --- A worker only ever writes inside its own copy of the project ----------------------------
#
# `--jobs N` gives each worker a private copy of the project and mutates the file inside it. The
# address it wrote to was the source's path taken relative to the project, used as-is -- so a
# source that was not under the project produced one beginning with "..", and the worker wrote
# through its own copy and out the other side. The mutation then never reached the copy the tests
# were about to run against, and every mutant came back SURVIVED.


#: A one-comparison source, so the parallel tests below produce a small, predictable mutant set.
SAFE_SRC = "func f(a, b) -> bool:\n\treturn a > b\n"


def _project_and_outside_source(tmp_path: Path) -> tuple[Path, Path]:
    """A project directory, and a .gd file that is its sibling rather than inside it."""
    project = tmp_path / "godot-project"
    project.mkdir()
    (project / "project.godot").write_text("[application]\n", encoding="utf-8")
    outside = tmp_path / "shared.gd"
    outside.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8")
    return project, outside


def test_a_source_outside_the_project_is_refused_rather_than_written_outside_the_copy(
    tmp_path: Path,
) -> None:
    # The regression test. Against the old address arithmetic this run finished quietly and
    # reported every mutant as SURVIVED -- a false survivor report, which is the single worst
    # thing this tool can produce -- while writing the mutants to a path outside every worker's
    # copy. Refusing is the honest answer: there is no copy of this file to isolate.
    project, outside = _project_and_outside_source(tmp_path)
    src = outside.read_text(encoding="utf-8")

    with pytest.raises(SourceOutsideProject, match="is not inside the project directory") as exc:
        run(str(project), str(outside), src, ProjectDirRecordingRunner(), jobs=4)
    # Named the way the "mutating ..." line printed just before it names the file. On POSIX the
    # two forms are equal, so the second check only bites on Windows.
    assert str(exc.value).startswith(f"{outside.as_posix()} is not inside")
    if str(outside) != outside.as_posix():
        assert str(outside) not in str(exc.value)


def test_the_refusal_says_how_to_proceed(tmp_path: Path) -> None:
    # A dead end with no way out is barely better than the wrong answer it replaced.
    project, outside = _project_and_outside_source(tmp_path)

    with pytest.raises(SourceOutsideProject) as caught:
        run(
            str(project),
            str(outside),
            outside.read_text(encoding="utf-8"),
            ProjectDirRecordingRunner(),
            jobs=2,
        )

    message = str(caught.value)
    assert "--project" in message
    assert "serially" in message


def test_a_source_on_another_drive_is_refused_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows only: a source and a project on different drives have no relative path at all, and
    # the old call raised a bare ValueError straight out of the run. Simulated rather than
    # requiring a second drive, so it guards the behaviour on every platform CI runs.
    project, outside = _project_and_outside_source(tmp_path)

    def no_common_root(self: Path, *args: object) -> Path:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(Path, "relative_to", no_common_root)
    with pytest.raises(SourceOutsideProject):
        run(
            str(project),
            str(outside),
            outside.read_text(encoding="utf-8"),
            ProjectDirRecordingRunner(),
            jobs=2,
        )


def test_a_source_inside_the_project_still_runs_in_parallel(tmp_path: Path) -> None:
    # The containment check must not cost the feature it protects: the ordinary layout, where the
    # source lives under the project, keeps working and each worker gets its own copy.
    project = tmp_path / "godot-project"
    (project / "src").mkdir(parents=True)
    inside = project / "src" / "player.gd"
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
    inside.write_text(src, encoding="utf-8")
    runner = ProjectDirRecordingRunner()

    result = run(str(project), str(inside), src, runner, jobs=2)

    assert result.outcomes, "the source should produce mutants"
    assert inside.read_text(encoding="utf-8") == src, "the real source must come back unchanged"
    worker_dirs = {seen for seen in runner.seen if seen != str(project)}
    assert worker_dirs, "mutants should have run in worker copies, not the real project"
    assert all(str(project) not in d or Path(d) != project for d in worker_dirs)


def test_a_source_outside_the_project_still_runs_serially(tmp_path: Path) -> None:
    # Serial evaluation mutates the real file in place and never needed a copy, so it is not
    # affected -- the refusal is scoped to the parallel path, not a new restriction on the tool.
    project, outside = _project_and_outside_source(tmp_path)
    src = outside.read_text(encoding="utf-8")

    result = run(str(project), str(outside), src, MarkerRunner(str(outside), "a >= b"))

    assert result.killed == 1
    assert outside.read_text(encoding="utf-8") == src


def test_a_deeply_nested_project_cannot_walk_back_onto_the_real_source_file(
    tmp_path: Path,
) -> None:
    # The worst case, and the reason this is not merely untidy. The `..` chain is as long as the
    # source's distance from the project, so a deeply nested --project produces more of them than
    # the temporary directory has depth. The walk then clamps at the drive (or filesystem) root and
    # the tail rebuilds the source's own absolute path -- so the write lands on the REAL file,
    # every worker races on it at once, and "never leave the project mutated" is void.
    real = tmp_path / "player.gd"
    src = SAFE_SRC
    real.write_text(src, encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "project"
    deep.mkdir(parents=True)

    with pytest.raises(SourceOutsideProject):
        run(str(deep), str(real), src, ProjectDirRecordingRunner(), jobs=4)

    assert real.read_text(encoding="utf-8") == src


# --- The source file is only ever replaced whole (crash-safe restore) -------------------------
#
# A mutation run rewrites the user's own source twice per mutant, and spends nearly all of its
# time between those two writes. Writing in place emptied the file before putting anything back,
# so a hard kill, a power cut, or a Ctrl-C landing in that window destroyed it. `_write_source`
# now stages the bytes in a sibling temporary file and renames it over the target, so the path
# always holds one complete version or the other.


class ProcessKilled(Exception):
    """Stands in for the process dying mid-write (a hard kill, a power cut, a Ctrl-C).

    Not an `OSError`, so nothing in `_write_source`'s fallback path swallows it -- it ends the
    write where it was raised, which is what a real kill does.
    """


def _kill_on_truncating_open(target: Path, real_open: Callable[..., Any]) -> Callable[..., Any]:
    """A stand-in for `open` that kills the process the instant `target` is opened for writing.

    Opening a file for ``"w"`` empties it before a single byte can be written back, so that
    instant *is* the window in which the user's source exists nowhere on disk. This stand-in makes
    the window fatal, so a write that still has one cannot pass.
    """

    def opener(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, Path)) and Path(file) == target and "w" in mode:
            real_open(file, mode, *args, **kwargs).close()  # truncate, as the real call would
            raise ProcessKilled("killed mid-write")
        return real_open(file, mode, *args, **kwargs)

    return opener


def test_a_write_never_opens_the_source_file_in_a_truncating_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression test, and the whole guarantee in one line: there is no moment at which the
    # user's source file has been emptied and not yet rewritten. The stand-in above makes any such
    # moment fatal.
    #
    # Against the old in-place write this fails outright -- the write opens the target for "w",
    # the stand-in fires, and the file is left at zero bytes, which is what a hard kill did to real
    # source code. The staged write opens only a sibling temporary file and renames it over the
    # target, so the stand-in never fires and the new content arrives whole.
    path = tmp_path / "player.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8", newline="")
    mutated = "func f(a, b) -> bool:\n\treturn a >= b\n"

    monkeypatch.setattr("builtins.open", _kill_on_truncating_open(path, open))
    _write_source(path, mutated, "\n")
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == mutated


def test_a_write_keeps_the_targets_permission_bits(tmp_path: Path) -> None:
    # A temporary file is created private to its owner. Renaming it over the target without
    # copying the target's mode across would silently tighten the source file's permissions -- a
    # file the whole team could read becoming owner-only, as a side effect of a test run.
    path = tmp_path / "modes.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8", newline="")
    os.chmod(path, 0o644)
    before = stat.S_IMODE(path.stat().st_mode)

    _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert stat.S_IMODE(path.stat().st_mode) == before


def test_a_completed_run_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    # The staging file lives beside the source, inside the user's own project. A completed run
    # must leave that directory exactly as it found it -- no stray files for the game engine to
    # scan or for the user to wonder about.
    path = tmp_path / "clean.gd"
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(src, encoding="utf-8", newline="")

    run(str(tmp_path), str(path), src, MarkerRunner(str(path), "a >= b"))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["clean.gd"]


def test_a_rename_blocked_by_another_process_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows the rename fails with PermissionError while ANY other process holds the target
    # open -- even only for reading, which an editor, an antivirus scanner, or the very test
    # engine gdmutant just launched all do routinely. Those holders let go in milliseconds, so a
    # couple of retries turn the common case into a non-event instead of a degraded write.
    path = tmp_path / "locked.gd"
    path.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8", newline="")
    real_replace = os.replace
    attempts: list[int] = []

    def flaky_replace(src: Any, dst: Any) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr("gdmutant.engine.loop.os.replace", flaky_replace)
    monkeypatch.setattr("gdmutant.engine.loop._REPLACE_BACKOFF", 0.0)
    _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert len(attempts) == 3, "the blocked rename should have been retried, not given up on"
    assert path.read_text(encoding="utf-8") == "func f(a, b) -> bool:\n\treturn a >= b\n"


def test_a_rename_that_never_unblocks_refuses_rather_than_writing_unsafely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lock that outlasts the retries used to fall back to the plain in-place write, on the
    # reasoning that a write as unsafe as the old one still beat no write at all. It does not: the
    # fallback truncates first, so it can leave the file empty (see the persistent-fault test
    # below). Refusing with the file intact is the only answer consistent with this module's
    # promise.
    path = tmp_path / "stuck.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")

    def always_blocked(src: Any, dst: Any) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("gdmutant.engine.loop.os.replace", always_blocked)
    monkeypatch.setattr("gdmutant.engine.loop._REPLACE_BACKOFF", 0.0)
    with pytest.raises(SourceWriteFailed, match="This write changed nothing"):
        _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert path.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stuck.gd"], "no temporary file left"


def test_the_retry_budget_is_six_tries_spread_over_about_a_second_and_a_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The two tests above prove the retry loop retries and that it eventually gives up. Neither
    # looks at the two numbers that decide whether the retries are worth having: how many tries,
    # and how long apart. A budget that spends one try, or that sleeps zero, still passes both --
    # and so does one that stalls the run for minutes. That is not a detail. Six tries spaced by
    # waits that grow one _REPLACE_BACKOFF at a time is what makes a virus scanner or a search
    # indexer holding the file a non-event rather than a failed run, and holding the total near a
    # second and a half is what keeps a genuinely stuck file from costing that much per mutant,
    # thousands of times over.
    #
    # Both numbers are read off a lock that never lets go, because that is the only case that
    # spends the whole budget. Nothing sleeps for real: time.sleep is replaced by a recorder, so
    # the schedule is checked at its true values instead of being zeroed out for speed.
    path = tmp_path / "held.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")
    tries = 0
    waits: list[float] = []

    def always_blocked(src: Any, dst: Any) -> None:
        nonlocal tries
        tries += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("gdmutant.engine.loop.os.replace", always_blocked)
    monkeypatch.setattr("gdmutant.engine.loop.time.sleep", waits.append)

    with pytest.raises(SourceWriteFailed):
        _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert tries == 6, "five tries inside the retry loop, then one last one that is allowed to fail"
    assert waits == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5]), (
        "each wait is one _REPLACE_BACKOFF longer than the last, so the whole budget is 1.5s"
    )
    assert path.read_text(encoding="utf-8") == original


def test_a_persistent_write_fault_leaves_the_source_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression test for the defect this module exists to prevent, reached from the other
    # side. A full disk fails the staged write's flush; the old code then fell back to the plain
    # in-place write, which truncates the file BEFORE writing, so the same full disk failed that
    # write too and left the user's source at zero bytes. One fault, both paths -- they write to
    # the same filesystem, so a single cause hitting both is the expected case, not a coincidence.
    #
    # Against the fallback this test fails with the file empty: the exact outcome the PR's title
    # promises can never happen.
    path = tmp_path / "fullDisk.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")

    def disk_full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("gdmutant.engine.loop.os.fsync", disk_full)
    with pytest.raises(SourceWriteFailed):
        _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert path.read_bytes() == original.encode(), "a failed write must not touch the source"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fullDisk.gd"]


def test_a_directory_that_cannot_hold_a_temporary_file_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no staging file there is no safe write to make, and the unsafe one is not on offer any
    # more. Refuse -- and name what is on disk rather than calling it untouched. Nothing was
    # opened, so the file is whole, but this helper is also what restores the original after a
    # mutant, and on that call "whole" means the mutant is still there.
    path = tmp_path / "notemp.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")

    def no_temp_files(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("gdmutant.engine.loop.tempfile.mkstemp", no_temp_files)
    with pytest.raises(SourceWriteFailed, match="not your original"):
        _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert path.read_text(encoding="utf-8") == original


def test_a_read_only_source_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    # A rename needs write permission on the DIRECTORY, not on the file, so the staged write would
    # happily replace a file whose permissions say "do not modify me" -- something the plain write
    # it replaces refused to do. Marking a file read-only is a deliberate instruction (Perforce
    # checkouts do it to every unopened file), so it is honoured.
    path = tmp_path / "readonly.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")
    os.chmod(path, stat.S_IREAD)
    if os.access(path, os.W_OK):  # pragma: no cover - root ignores permission bits entirely
        pytest.skip("this account can write read-only files, so the bit proves nothing here")

    try:
        with pytest.raises(SourceWriteFailed, match="read-only"):
            _write_source(path, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")
        assert path.read_text(encoding="utf-8") == original
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # let tmp_path clean up


def test_a_source_that_turns_read_only_mid_mutant_says_the_mutant_is_the_one_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The read-only refusal is the third thing that raises SourceWriteFailed, and it used to be the
    # one that told the user nothing about what they were left with. It is reached from the restore
    # write as readily as from the mutate write: _write_source runs twice per mutant, so a file
    # whose read-only bit is set in the window between them fails on the way back, with the mutant
    # sitting on disk. That window is not hypothetical on a machine where Perforce reverts a
    # checkout or a build step marks generated sources read-only, and "your file is read-only" is
    # a sentence a reader takes as being about their own source.
    #
    # Driven through the real two-write sequence, so the assertion is about the case a user hits
    # rather than about one isolated call. os.access is forced rather than the bit being chmod'ed,
    # because a real chmod proves nothing under an account that ignores permission bits, and the
    # test above skips there. A test that skips where it matters is this change's own subject.
    path = tmp_path / "checkedout.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    mutant = "func f(a, b) -> bool:\n\treturn a >= b\n"
    path.write_text(original, encoding="utf-8", newline="")

    _write_source(path, mutant, "\n")  # what _run_one does first: put the mutant in

    real_access = os.access

    def read_only_now(target: Any, mode: int) -> bool:
        if Path(target) == path.resolve() and mode == os.W_OK:
            return False
        return real_access(target, mode)

    monkeypatch.setattr("gdmutant.engine.loop.os.access", read_only_now)

    with pytest.raises(SourceWriteFailed) as refusal:
        _write_source(path, original, "\n")  # what _run_one does in its finally: put it back

    assert path.read_text(encoding="utf-8") == mutant, (
        "the premise of this test: the restore failed, so the MUTANT is what is sitting there"
    )
    message = str(refusal.value)
    assert "the mutant is what is on disk now" in message, (
        "the refusal must name what the user is actually left holding"
    )
    assert "not your original" in message, "and say plainly that it is not their own source"
    assert "from git" in message, "and point at the one place the original can be got back"


def test_a_missing_target_is_still_written(tmp_path: Path) -> None:
    # The permission check and the mode copy both only apply to a file that is actually there.
    # A target that does not exist yet is simply created -- no mode to preserve, nothing to refuse.
    path = tmp_path / "brandnew.gd"

    _write_source(path, "func f(a, b) -> bool:\n\treturn a > b\n", "\n")

    assert path.read_text(encoding="utf-8") == "func f(a, b) -> bool:\n\treturn a > b\n"


def test_a_symlinked_source_is_written_through_rather_than_replaced(tmp_path: Path) -> None:
    # Renaming over a symlink swaps the LINK for a regular file and leaves the file it names
    # untouched -- so a project that symlinks a shared script would have the link silently
    # destroyed and the real source never mutated, nor restored. Resolving the link first keeps
    # the behaviour the plain in-place write had, which wrote straight through it.
    real = tmp_path / "real.gd"
    real.write_text("func f(a, b) -> bool:\n\treturn a > b\n", encoding="utf-8", newline="")
    link = tmp_path / "link.gd"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this platform or account cannot create symlinks")

    _write_source(link, "func f(a, b) -> bool:\n\treturn a >= b\n", "\n")

    assert link.is_symlink(), "the symlink itself must survive the write"
    assert real.read_text(encoding="utf-8") == "func f(a, b) -> bool:\n\treturn a >= b\n"


@dataclass
class SlowRunner:
    """A runner whose suite takes `cost` seconds of make-believe: it raises `SuiteTimeout` for any
    budget under that and passes for any budget over it. Its baseline run advances `clock` by
    `wall`, so the engine measures a real-looking wall-clock to decompose.

    This is the whole question a wall-clock budget cannot answer on its own. A suite that needs 40s
    and a suite that needs forever both look identical to a 20s budget, and every other mutation
    tester records the same kill for both."""

    cost: float
    baseline: SuiteResult
    clock: list[float]
    wall: float
    budgets: list[float | None] = field(default_factory=list)
    failing: bool = False

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        if timeout is None:  # the baseline, run on the runner's own budget
            self.clock[0] += self.wall
            return self.baseline
        self.budgets.append(timeout)
        if timeout < self.cost:
            raise SuiteTimeout(f"took longer than {timeout:g}s")
        return SuiteResult(tests=1, failures=1 if self.failing else 0, errors=0)


def _slow(
    monkeypatch: pytest.MonkeyPatch, cost: float, *, net: float = 40.0, failing: bool = False
) -> SlowRunner:
    """A `SlowRunner` on a fake clock, whose baseline takes 50s of which `net` was tests.

    The clock is what makes the decomposition real here: `TimeBudget` clamps the reported test
    time to the wall-clock it was measured against, so a baseline that returns instantly would
    have no startup to measure and every budget would land on the floor.
    """
    from gdmutant.engine import loop as loop_mod

    clock = [0.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: clock[0])
    return SlowRunner(
        cost=cost, baseline=_measured_baseline(net), clock=clock, wall=50.0, failing=failing
    )


def _measured_baseline(net: float) -> SuiteResult:
    """A baseline result whose report says its tests took `net` seconds, as a real one does."""
    return SuiteResult(
        tests=1,
        failures=0,
        errors=0,
        suites=(ReportedSuite("only", tests=1, time=net, file="res://test/only.gd"),),
    )


def _one_mutant(tmp_path: Path) -> tuple[str, str]:
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    return src, _write(tmp_path, "f.gd", src)


def test_a_mutant_that_runs_long_is_re_run_before_it_is_called_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mutant needs more time than the first budget allows and less than the confirmation
    # budget. Every other mutation tester records that as a kill. gdmutant runs it again and finds
    # out it was never hanging, so the verdict is the real one: SURVIVED.
    src, path = _one_mutant(tmp_path)
    budget = TimeBudget(net=40.0, overhead=10.0, measured=True)  # a 50s baseline, 40s of it tests
    assert budget.first() < 120.0 < budget.confirmation()  # 98.0 < 120.0 < 418.0
    runner = _slow(monkeypatch, cost=120.0)

    result = run(str(tmp_path), path, src, runner)

    assert [o.verdict for o in result.outcomes] == [Verdict.SURVIVED]
    assert result.reprieved == 1  # it would have been a false kill
    assert result.timeouts == 0
    assert runner.budgets == [budget.first(), budget.confirmation()]  # exactly two runs, no more


def test_a_mutant_that_hangs_through_both_budgets_stays_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the same rule. A real hang pays the first budget and then the confirmation
    # budget, and is recorded as a TIMEOUT that something actually checked.
    src, path = _one_mutant(tmp_path)
    runner = _slow(monkeypatch, cost=10_000.0)

    result = run(str(tmp_path), path, src, runner)

    assert [o.verdict for o in result.outcomes] == [Verdict.TIMEOUT]
    assert result.confirmed_timeouts == 1
    assert result.reprieved == 0


def test_a_confirmation_run_reports_the_verdict_it_reaches_not_just_a_reprieve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reprieved mutant is not automatically a survivor. Whatever the second run says is the
    # answer, kills included, so the score is built from the run that finished.
    src, path = _one_mutant(tmp_path)
    runner = _slow(monkeypatch, cost=120.0, failing=True)

    result = run(str(tmp_path), path, src, runner)

    assert [o.verdict for o in result.outcomes] == [Verdict.KILLED]
    assert result.reprieved == 1


def test_an_explicit_timeout_is_never_second_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Someone who names a number meant it. The mutant runs once, on that number, and a timeout
    # there stands unconfirmed rather than quietly buying itself a longer run.
    src, path = _one_mutant(tmp_path)
    runner = _slow(monkeypatch, cost=120.0)

    result = run(str(tmp_path), path, src, runner, timeout=30.0)

    assert [o.verdict for o in result.outcomes] == [Verdict.TIMEOUT]
    assert result.confirmed_timeouts == 0  # nothing checked it, and the summary says so
    assert runner.budgets == [30.0]


def test_the_overhead_is_measured_from_the_report_not_assumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The measurement the whole formula rests on. Two baselines take the SAME wall-clock; they
    # differ only in what their reports say the tests took. The budgets must differ accordingly,
    # which is only possible if the startup is read off the report rather than guessed at.
    from gdmutant.engine import loop as loop_mod

    src, path = _one_mutant(tmp_path)

    def budget_for(net: float) -> float:
        clock = [0.0]
        monkeypatch.setattr(loop_mod.time, "monotonic", lambda: clock[0])

        @dataclass
        class Ticking:
            result: SuiteResult
            seen: list[float | None] = field(default_factory=list)

            def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
                self.seen.append(timeout)
                clock[0] += 50.0  # every run, baseline included, takes 50s of wall-clock
                return self.result if timeout is None else SuiteResult(1, 0, 0)

        runner = Ticking(_measured_baseline(net))
        run(str(tmp_path), path, src, runner)
        _, first, *_ = runner.seen
        assert first is not None
        return first

    # Same 50s wall-clock both times. 40s of tests leaves 10s of startup; 5s of tests leaves 45s.
    assert budget_for(40.0) == 2.0 * 40.0 + 8.0 + 10.0  # 98.0
    assert budget_for(5.0) == 2.0 * 5.0 + 8.0 + 45.0  # 63.0
    # And neither is the old answer, which multiplied the startup along with everything else.
    assert 2.0 * 40.0 + 8.0 + 10.0 != 10.0 * 50.0


def _coverage_with_selection(*file_sets: tuple[str, ...]) -> _RunCoverage:
    """A coverage plan whose mutants were sent to `file_sets`, and one that runs everything."""
    plans: dict[int, MutantPlan] = {index: MutantPlan(files=f) for index, f in enumerate(file_sets)}
    plans[len(file_sets)] = MutantPlan()  # a whole-suite mutant, which asks for no file's time
    return _RunCoverage(files={"f.gd": _FileCoverage(plans=plans)})


def test_the_run_says_nothing_when_per_file_budgets_work() -> None:
    # The quiet case, and the one that has to stay quiet: every file selection chose has a
    # duration, so a selected mutant really is budgeted for its own files.
    lines: list[str] = []
    budget = TimeBudget(net=10.0, overhead=2.0, per_file={"a.gd": 1.0, "b.gd": 2.0}, measured=True)
    _budget_note(budget, _coverage_with_selection(("a.gd",), ("a.gd", "b.gd")), lines.append)
    assert lines == []


def test_the_run_says_so_when_the_report_named_no_durations() -> None:
    lines: list[str] = []
    _budget_note(TimeBudget(overhead=5.0), _coverage_with_selection(("a.gd",)), lines.append)
    assert lines == [
        "budget: the baseline's report did not say how long its tests took, so the whole "
        "baseline wall-clock sets each mutant's time budget rather than the test time alone."
    ]


def test_the_run_says_so_when_a_selected_file_has_no_known_duration() -> None:
    # The failure this exists to make visible. The recorder's spelling of a test file and the
    # report's need not match, and when they do not, every selected mutant quietly falls back to
    # the whole suite's time: the run stays correct and the saving simply never happens. A
    # capability that silently does nothing is the shape this project keeps finding.
    lines: list[str] = []
    budget = TimeBudget(net=10.0, overhead=2.0, per_file={"a.gd": 1.0}, measured=True)
    _budget_note(budget, _coverage_with_selection(("a.gd", "b.gd"), ("c.gd",)), lines.append)
    assert lines == [
        "budget: 2 of the 3 test files selection chose are named differently in the baseline's "
        "report, so a selected mutant is budgeted for the whole suite's test time rather than "
        "its own files'. Verdicts are unaffected."
    ]


def test_the_budget_note_is_silent_under_an_explicit_timeout() -> None:
    # The user named a number and every mutant gets it, so neither half of the note applies.
    # Reporting that the baseline's report gave no durations would be true and misleading at once:
    # it is not what set the budget.
    lines: list[str] = []
    _budget_note(TimeBudget(fixed=30.0), _coverage_with_selection(("a.gd",)), lines.append)
    assert lines == []


def test_the_budget_note_is_silent_with_no_coverage_analysis_or_no_progress() -> None:
    # Coverage analysis off means no selection, so there is nothing about per-file budgets to say.
    # And a caller with no progress callback asked for silence.
    lines: list[str] = []
    _budget_note(TimeBudget(overhead=5.0), None, lines.append)
    assert lines == []
    _budget_note(TimeBudget(overhead=5.0), _coverage_with_selection(("a.gd",)), None)


# --- the budget read off a real baseline, and the flags that survive a second run ---------------
# Everything below was added because a mutation run found the line it covers could be changed
# with the whole suite staying green. A budget nothing pins is a budget that can be wrong.


def _reported(*suites: ReportedSuite) -> SuiteResult:
    return SuiteResult(tests=sum(s.tests for s in suites), failures=0, errors=0, suites=suites)


def test_the_budget_carries_the_reports_per_file_times() -> None:
    # Without this the whole per-file half of the budget is inert: every selected mutant falls
    # back to the whole suite's time and the run looks exactly the same.
    baseline = _reported(
        ReportedSuite("a", tests=1, time=1.0, file="res://a.gd"),
        ReportedSuite("b", tests=1, time=3.0, file="res://b.gd"),
    )
    budget = _baseline_budget(baseline, wall=10.0, timeout=None)

    assert budget.per_file == {"res://a.gd": 1.0, "res://b.gd": 3.0}
    # And the map is really used: this mutant is budgeted for a.gd alone, not for all 4 seconds.
    assert budget.first(("res://a.gd",)) == 2.0 * 1.0 + 8.0 + 6.0  # 16.0
    assert budget.first() == 2.0 * 4.0 + 8.0 + 6.0  # 22.0


def test_the_startup_floors_at_zero_when_a_report_claims_more_time_than_the_run_took() -> None:
    # A framework may report more test time than the wall-clock (tests that overlap). The startup
    # is then zero, not some minimum: inventing one would add seconds to every budget on every
    # project that does it, which is the kind of number nobody chose and nobody can defend.
    budget = _baseline_budget(_reported(ReportedSuite("a", tests=1, time=9.0)), 5.0, timeout=None)

    assert budget.net == 5.0  # clamped to the wall-clock it was measured against
    assert budget.overhead == 0.0
    assert budget.first() == 2.0 * 5.0 + 8.0 + 0.0  # 18.0


def test_any_reported_time_at_all_counts_as_measured() -> None:
    # The line between "this report said how long its tests took" and "it did not" is exactly
    # zero, and both sides of it change the whole formula. A report saying nothing must fall back
    # to multiplying the wall-clock. A report saying a fraction of a second must not: GUT really
    # does report 0.0004 s for a small suite, so a threshold anywhere above zero would switch the
    # decomposition off on a framework that supports it.
    silent = _baseline_budget(_reported(ReportedSuite("a", tests=1)), 5.0, timeout=None)
    assert silent.measured is False
    assert silent.first() == 10.0 * 5.0  # the whole wall-clock, multiplied, as it always was

    tiny = _baseline_budget(_reported(ReportedSuite("a", tests=1, time=0.5)), 5.0, timeout=None)
    assert tiny.measured is True
    assert tiny.first() == 2.0 * 0.5 + 8.0 + 4.5  # 13.5


@dataclass
class _SelectingSlow:
    """A `FileSelecting` runner whose suite needs `cost` seconds: any smaller budget times out.

    Records every (files, budget) pair it was handed, which is how a test can see *which* budget
    reached the runner rather than only what verdict came back.
    """

    cost: float
    failures: int = 0
    budgets: list[tuple[tuple[str, ...] | None, float | None]] = field(default_factory=list)

    def _answer(self, files: tuple[str, ...] | None, timeout: float | None) -> SuiteResult:
        self.budgets.append((files, timeout))
        if timeout is not None and timeout < self.cost:
            raise SuiteTimeout(f"needed {self.cost:g}s")
        return SuiteResult(tests=2, failures=self.failures, errors=0)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        return self._answer(None, timeout)

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        return self._answer(tuple(files), timeout)

    # The rest of `FileSelecting`, never reached by these tests.
    def install_windows(self, project_dir: str, recorder_dir: str) -> None: ...  # pragma: no cover
    def run_markers_files(  # pragma: no cover
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult: ...


@dataclass
class _TrustSpy:
    """Stands in for the runner a `_Trust` confirms against, and records the budget it was given.

    Separate from the runner under test on purpose: in production they are one object, and a test
    that shared them could not tell a confirmation run apart from a mutant's own run, which is the
    one thing these tests are looking at.
    """

    clean: bool = True
    budgets: list[float | None] = field(default_factory=list)

    def run_selected(
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult:
        self.budgets.append(timeout)
        return SuiteResult(tests=2, failures=0 if self.clean else 1, errors=0)

    def install_windows(self, project_dir: str, recorder_dir: str) -> None: ...  # pragma: no cover
    def run_markers_files(  # pragma: no cover
        self, project_dir: str, files: Sequence[str], timeout: float | None = None
    ) -> SuiteResult: ...


#: A budget where the two scopes are far apart, so a test can tell which one reached the runner.
#: first(("a.gd",)) = 15.0, confirmation = 23.0; first(None) = 73.0, confirmation = 313.0.
_SPLIT_BUDGET = TimeBudget(net=30.0, overhead=5.0, per_file={"a.gd": 1.0}, measured=True)


def _selected_mutant(tmp_path: Path, runner: object, trust: object) -> _Evaluation:
    src, path = _one_mutant(tmp_path)
    return _Evaluation(str(tmp_path), path, src, runner, _SPLIT_BUDGET, trust)  # type: ignore[arg-type]


_A_MUTANT = Mutant("f.gd", Span(2, 9, 2, 10), "comparison", ">", ">=")
_MUTATED = "func f(a, b) -> bool:\n\treturn a >= b\n"


def test_a_selected_kill_is_confirmed_on_its_own_files_budget(tmp_path: Path) -> None:
    # The confirmation runs the same chosen test files, so it gets the budget those files earn,
    # not the whole suite's. Handing it the whole suite's would let a set that really is too slow
    # pass confirmation, and the mutant's kill would be believed off files that cannot be trusted.
    assert _SPLIT_BUDGET.first(("a.gd",)) == 15.0
    assert _SPLIT_BUDGET.first() == 73.0
    runner = _SelectingSlow(cost=0.0, failures=1)  # a kill, which is what sends it to the trust
    spy = _TrustSpy(clean=True)
    ctx = _selected_mutant(tmp_path, runner, _Trust(spy))  # type: ignore[arg-type]

    outcome = _evaluate(ctx, _A_MUTANT, _MUTATED, MutantPlan(files=("a.gd",)))

    assert spy.budgets == [15.0]
    assert outcome.verdict is Verdict.KILLED
    assert outcome.order_coupled is False


def test_running_past_the_budget_is_remembered_across_an_order_coupled_fallback(
    tmp_path: Path,
) -> None:
    # The selected run goes over its budget and the confirmation pass rescues it, then the chosen
    # files turn out not to pass unmutated so the whole suite decides instead. The second run was
    # comfortably inside its budget, and the fact that the first one was not must survive: it is
    # the count that says how close the tight budget came to being wrong.
    runner = _SelectingSlow(cost=20.0, failures=1)
    ctx = _selected_mutant(tmp_path, runner, _Trust(_TrustSpy(clean=False)))  # type: ignore[arg-type]

    outcome = _evaluate(ctx, _A_MUTANT, _MUTATED, MutantPlan(files=("a.gd",)))

    assert [b for _, b in runner.budgets] == [15.0, 23.0, 73.0]  # over, rescued, then everything
    assert outcome.verdict is Verdict.KILLED
    assert outcome.order_coupled is True
    assert outcome.over_budget is True


def test_running_past_the_budget_is_remembered_across_the_self_check(tmp_path: Path) -> None:
    # Same fact on the other road to a verdict. A self-checked mutant runs the whole suite a
    # second time, well inside its budget, and that must not erase the reprieve the first run
    # earned.
    runner = _SelectingSlow(cost=20.0, failures=0)
    ctx = _selected_mutant(tmp_path, runner, None)

    outcome = _evaluate(ctx, _A_MUTANT, _MUTATED, MutantPlan(files=("a.gd",), self_check=True))

    assert [b for _, b in runner.budgets] == [15.0, 23.0, 73.0]  # over, rescued, then the check
    assert outcome.verdict is Verdict.SURVIVED
    assert outcome.self_checked is True
    assert outcome.over_budget is True
