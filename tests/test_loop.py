"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import MarkerRunner

from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.engine.adapter import Adapter
from gdmutant.engine.loop import (
    BaselineFailed,
    MutantOutcome,
    ProgressStyle,
    SourceOutsideProject,
    SourceWriteFailed,
    Verdict,
    _contention_budget,
    _derive_timeout,
    _detect_eol,
    _format_duration,
    _plain_beat_every,
    _Progress,
    _progress_line,
    _progress_plan,
    _progress_start,
    _write_source,
)
from gdmutant.engine.loop import run as _run
from gdmutant.engine.loop import run_paths as _run_paths
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import TableOperator
from gdmutant.engine.runner import SuiteResult, SuiteTimeout
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
    assert all(t == _derive_timeout(0.05) for t in mutant_timeouts)
    assert all(t != _derive_timeout(5.05) for t in mutant_timeouts)  # prepare NOT folded in


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


def test_derive_timeout_formula() -> None:
    # Per-mutant budget = baseline * 10, floored at 10s and capped at 600s (a slow suite is never
    # worse off than the historical flat default; a fast suite gets a tight budget so a hang is
    # caught in seconds).
    assert _derive_timeout(0.0) == 10.0  # floor
    assert _derive_timeout(0.5) == 10.0  # 5s -> floored to 10
    assert _derive_timeout(4.0) == 40.0  # 10x
    assert _derive_timeout(100.0) == 600.0  # capped


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


def test_progress_fires_a_heartbeat_then_a_verdict_per_mutant_in_order(tmp_path: Path) -> None:
    # Progress fires the baseline notice, then for each mutant that runs: a "running (<=Ns)"
    # heartbeat BEFORE (so a hang shows on a specific mutant, not a frozen terminal) and a verdict
    # line AFTER — pinned exactly so a mutated separator, index base, budget, or verdict label is
    # caught. Source has 3 mutants: >, and, <. Explicit timeout keeps the budget deterministic.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    lines: list[str] = []
    runner = MarkerRunner(target=path, kill_marker=">=")
    run(str(tmp_path), path, src, runner, timeout=10.0, progress=lines.append)
    assert lines[0] == "running the unmutated (baseline) suite ..."
    # The plan line follows the baseline; the baseline's own wall-clock is nondeterministic, so
    # pin the part that isn't. The trailing forced heartbeat and the closing wall-clock are timed
    # too, so the verdict block is sliced out between them.
    assert lines[1].startswith("3 mutants to run.") and lines[1].endswith("capped at 10s.")
    assert lines[-2].startswith("… 3/3 done in ") and lines[-1].startswith("Done in ")
    assert lines[2:-2] == [
        f"[1/3] {path}:2:11  > -> >=  running (<=10s) ...",
        f"[1/3] {path}:2:11  > -> >=  ... killed",
        f"[2/3] {path}:2:15  and -> or  running (<=10s) ...",
        f"[2/3] {path}:2:15  and -> or  ... survived",
        f"[3/3] {path}:2:21  < -> <=  running (<=10s) ...",
        f"[3/3] {path}:2:21  < -> <=  ... survived",
    ]


def test_progress_reports_invalid_and_error_verdicts(tmp_path: Path) -> None:
    # Every mutant is reported regardless of verdict — an invalid (unparseable) mutant labelled
    # "invalid", and a mutant whose runner raises labelled "error" (not only the ones that pass).
    src = "func f(a, b) -> bool:\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    invalid: list[str] = []
    bad = TableOperator("bad", {">": ("))",)})
    run(
        str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=(bad,), progress=invalid.append
    )
    # An invalid mutant never runs, so it gets NO heartbeat — only the verdict line. It is also
    # not a timing *sample*: counting a mutant that never reached the suite would make the closing
    # wall-clock's "N mutants" a fiction, so the run reports 0 of them.
    assert invalid[0] == "running the unmutated (baseline) suite ..."
    assert invalid[1].startswith("1 mutant to run.")
    assert invalid[2] == f"[1/1] {path}:2:11  > -> ))  ... invalid"
    assert invalid[-1].startswith("Done in ") and "0 mutants, none timed out." in invalid[-1]

    # An erroring mutant DID run, so it gets the heartbeat then the error verdict.
    errored: list[str] = []
    run(str(tmp_path), path, src, RaiseAfterBaselineRunner(), timeout=10.0, progress=errored.append)
    assert errored[0] == "running the unmutated (baseline) suite ..."
    assert errored[1].startswith("1 mutant to run.")
    assert errored[2:4] == [
        f"[1/1] {path}:2:11  > -> >=  running (<=10s) ...",
        f"[1/1] {path}:2:11  > -> >=  ... error",
    ]


def test_progress_lines_render_a_deletion_as_deleted() -> None:
    # The stderr progress heartbeat + verdict line are the most-seen render surface, so a deletion
    # (empty replacement) must show `not -> (deleted)` there too, not a dangling arrow.
    deletion = Mutant("f.gd", Span(2, 8, 2, 11), "logical-not", "not", "")
    start = _progress_start(1, 1, deletion, 10.0)
    line = _progress_line(1, 1, MutantOutcome(deletion, Verdict.SURVIVED))
    assert start == "[1/1] f.gd:2:8  not -> (deleted)  running (<=10s) ..."
    assert line == "[1/1] f.gd:2:8  not -> (deleted)  ... survived"


def test_format_duration_scales_seconds_minutes_hours() -> None:
    assert _format_duration(0) == "0s"
    assert _format_duration(9) == "9s"
    assert _format_duration(59.4) == "59s"  # rounds to whole seconds
    assert _format_duration(60) == "1m 0s"
    assert _format_duration(135) == "2m 15s"  # the issue's own example
    assert _format_duration(3780) == "1h 3m"


def test_progress_plan_states_the_work_and_the_cap_without_forecasting() -> None:
    # The pre-run line is facts only. "each mutant is capped at 30s" is the load-bearing clause: it
    # tells someone at a still terminal how long silence is normal, which is the pacing job the old
    # `estimated ≈` figure was doing badly. No total duration appears anywhere in it.
    line = _progress_plan(runnable=18, total=18, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=1)
    assert line == "18 mutants to run. Baseline suite 1.4s; each mutant is capped at 30s."


def test_progress_plan_counts_ignored_separately() -> None:
    line = _progress_plan(runnable=18, total=21, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=1)
    assert line.startswith("18 mutants to run (3 ignored). ")


def test_progress_plan_names_the_worker_count() -> None:
    line = _progress_plan(runnable=18, total=18, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=4)
    assert line.endswith(" Running 4 at a time.")


def test_progress_plan_names_the_cap_the_parallel_path_really_enforces() -> None:
    # Under `--jobs N` the run scales each mutant's budget by the worker count, so the unscaled
    # figure this line used to print understated the real worst case by up to N times — in the one
    # clause whose whole job is telling someone at a still terminal how long silence is normal.
    # Derived, never a literal: a hardcoded number is exactly how the message drifted from the code.
    expected = _contention_budget(30.0, min(4, 18))
    line = _progress_plan(runnable=18, total=18, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=4)
    assert f"each mutant is capped at {expected:g}s." in line
    assert expected == 120.0  # and the scaling is real: 4 workers, not the 30s serial figure


def test_progress_plan_still_names_a_real_cap_when_nothing_is_runnable() -> None:
    # A file whose mutants are all `# gdmutant: ignore`d reaches this line with a runnable count of
    # zero, and zero workers would scale the budget to "capped at 0s" — a figure that is not just
    # useless but visibly wrong, in the line a first-time user reads before the silence starts.
    line = _progress_plan(runnable=0, total=3, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=1)
    assert line.startswith("0 mutants to run (3 ignored). ")
    assert "each mutant is capped at 30s." in line


def test_progress_plan_never_names_a_scaled_cap_the_serial_path_will_not_apply() -> None:
    # The mirror of the case above. Serial runs enforce the unscaled budget, and one worker cannot
    # contend with itself — so `--jobs 4` on a single runnable mutant is a serial run wearing a
    # flag, and the announcement must not inflate its cap fourfold.
    line = _progress_plan(runnable=1, total=1, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=4)
    assert "each mutant is capped at 30s." in line


def test_progress_plan_is_singular_for_one_mutant() -> None:
    line = _progress_plan(runnable=1, total=1, baseline_secs=2.0, per_mutant_timeout=20.0, jobs=1)
    assert line.startswith("1 mutant to run. ")


def test_progress_plan_never_predicts_a_finish_time() -> None:
    # The whole point of the change. Nine surveyed mutation testers forecast an absolute duration
    # before the work starts; none of them do. Pin the absence so it cannot creep back.
    line = _progress_plan(runnable=99, total=99, baseline_secs=1.4, per_mutant_timeout=30.0, jobs=1)
    for forecast in ("estimated", "≈", "at least", "left", "ETA"):
        assert forecast not in line


def _clock(style: ProgressStyle, total: int, lines: list[str]) -> _Progress:
    clock = _Progress(emit=lines.append, style=style, baseline_secs=1.4)
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
    assert lines[-1] == "… 3/18 done in 0s — 1 survived, 1 timed out."
    assert "left" not in lines[-1] and "~" not in lines[-1]


def test_heartbeat_waits_for_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # At most one line every 30s, so the heartbeat can never become the noise it exists to prevent.
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.RICH, total=100, lines=lines)
    clock.record(Verdict.KILLED, 1.0)
    assert lines == []  # far too soon
    now[0] += loop_mod._HEARTBEAT_SECS
    clock.record(Verdict.KILLED, 1.0)
    assert lines == ["… 2/100 done in 30s — 0 survived, 0 timed out."]


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
    assert lines == ["… 10/100 done in 10m 0s — 0 survived, 0 timed out."]


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
    assert lines == ["… 4/4 done in 0s — 0 survived, 0 timed out."]


def test_progress_style_none_silences_the_heartbeat_but_not_the_closing_line() -> None:
    lines: list[str] = []
    clock = _clock(ProgressStyle.NONE, total=2, lines=lines)
    clock.record(Verdict.KILLED, 1.0)
    clock.beat(force=True)
    assert lines == []
    clock.finish()
    assert lines[0].startswith("Done in ")  # a fact about the run, not progress chatter


def test_a_clock_with_no_emitter_does_nothing() -> None:
    clock = _Progress(emit=None, style=ProgressStyle.RICH, baseline_secs=1.0)
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
    assert lines == [
        "Done in 6m 32s — 18 mutants, 8 timed out (4m 0s of that). Baseline suite 1.4s."
    ]


def test_closing_line_says_so_when_nothing_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from gdmutant.engine import loop as loop_mod

    now = [1000.0]
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: now[0])
    lines: list[str] = []
    clock = _clock(ProgressStyle.NONE, total=1, lines=lines)
    clock.record(Verdict.KILLED, 2.0)
    now[0] += 25.0
    clock.finish()
    assert lines == ["Done in 25s — 1 mutant, none timed out. Baseline suite 1.4s."]


def test_timeout_cost_is_measured_not_multiplied_out() -> None:
    # `timeouts × budget` would be wrong on the --jobs path, where the budget is scaled by the
    # worker count and the waits overlap. Only the real elapsed time is true on both paths.
    clock = _Progress(emit=None, style=ProgressStyle.NONE, baseline_secs=1.0)
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
    assert lines[-1].startswith("Done in ") and "1 mutant, none timed out." in lines[-1]


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
    assert sum(line.endswith("capped at 10s.") for line in lines) == 2  # one plan line per file


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
    assert f"mutating {a} ..." in lines and f"mutating {b} ..." in lines
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
    assert any(line.endswith("killed") for line in lines)  # parallel emitted verdict progress lines
    # The original project file is NEVER mutated in the parallel path — only the worker copies are.
    assert Path(parallel_path).read_text(encoding="utf-8") == src


def test_parallel_scales_the_per_mutant_timeout_by_worker_count(tmp_path: Path) -> None:
    # Soundness on the timeout axis: the per-mutant budget is derived from the SERIAL baseline, but
    # W workers contend for CPU so each suite runs slower in wall-clock. Scaling the budget by the
    # worker count keeps a genuinely-passing suite from crossing its timeout under load and being
    # misrecorded as a (killed) TIMEOUT — which would silently hide a survivor.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner, timeout=5.0, jobs=2)
    baseline, *mutant_timeouts = runner.seen
    assert baseline is None  # the baseline still uses the runner's own budget
    assert len(mutant_timeouts) == 3  # every mutant ran
    # worker_count = min(jobs=2, mutants=3) = 2, so each budget is the 5.0s serial value x2.
    assert all(t == 5.0 * 2 for t in mutant_timeouts)


def test_the_announced_cap_is_the_cap_the_run_hands_the_runner(tmp_path: Path) -> None:
    # The two facts, compared against each other in one real run: what the pre-run line PROMISES and
    # what the runner is actually GIVEN. Asserting the promise on its own is what let the message
    # drift to a quarter of the enforced value — the number was checked, the agreement never was.
    src = "func f(a, b) -> bool:\n\treturn a > b and a < b\n"  # 3 runnable mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    lines: list[str] = []
    run(str(tmp_path), path, src, runner, timeout=5.0, jobs=2, progress=lines.append)

    plan = next(line for line in lines if "each mutant is capped at" in line)
    announced = float(plan.split("each mutant is capped at ")[1].split("s.")[0])
    _, *mutant_timeouts = runner.seen  # drop the baseline, which uses the runner's own budget
    assert mutant_timeouts and all(t == announced for t in mutant_timeouts)


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

    with pytest.raises(SourceOutsideProject, match="is not inside the project directory"):
        run(str(project), str(outside), src, ProjectDirRecordingRunner(), jobs=4)


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
    # more. Refuse, and say the source is untouched -- which it is, because nothing was opened.
    path = tmp_path / "notemp.gd"
    original = "func f(a, b) -> bool:\n\treturn a > b\n"
    path.write_text(original, encoding="utf-8", newline="")

    def no_temp_files(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("gdmutant.engine.loop.tempfile.mkstemp", no_temp_files)
    with pytest.raises(SourceWriteFailed, match="left untouched"):
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
