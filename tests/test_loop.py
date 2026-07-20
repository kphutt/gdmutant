"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import MarkerRunner

from gdmutant.adapters.gdscript import ADAPTER
from gdmutant.engine.adapter import Adapter
from gdmutant.engine.loop import (
    BaselineFailed,
    MutantOutcome,
    Verdict,
    _derive_timeout,
    _format_duration,
    _progress_estimate,
    _progress_line,
    _progress_start,
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
    # clock starts, so its cost never inflates the derived per-mutant timeout or the ETA (LOD-213).
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
    assert "preparing the project (one-time) ..." in messages
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
    # The pre-run estimate (LOD-110) follows the baseline; its wall-clock timing is
    # nondeterministic, so pin its shape, not the exact seconds.
    assert lines[1].startswith("3 mutants;") and "estimated ≈" in lines[1]
    assert lines[2:] == [
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
    # An invalid mutant never runs, so it gets NO heartbeat — only the verdict line. (lines[1] is
    # the LOD-110 estimate, timing-dependent, so asserted by shape.)
    assert invalid[0] == "running the unmutated (baseline) suite ..."
    assert invalid[1].startswith("1 mutant;") and "estimated ≈" in invalid[1]
    assert invalid[2:] == [f"[1/1] {path}:2:11  > -> ))  ... invalid"]

    # An erroring mutant DID run, so it gets the heartbeat then the error verdict.
    errored: list[str] = []
    run(str(tmp_path), path, src, RaiseAfterBaselineRunner(), timeout=10.0, progress=errored.append)
    assert errored[0] == "running the unmutated (baseline) suite ..."
    assert errored[1].startswith("1 mutant;") and "estimated ≈" in errored[1]
    assert errored[2:] == [
        f"[1/1] {path}:2:11  > -> >=  running (<=10s) ...",
        f"[1/1] {path}:2:11  > -> >=  ... error",
    ]


def test_progress_lines_render_a_deletion_as_deleted() -> None:
    # The stderr progress heartbeat + verdict line are the most-seen render surface, so a deletion
    # (empty replacement) must show `not -> (deleted)` there too, not a dangling arrow (LOD-131).
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


def test_progress_estimate_reports_count_and_eta_from_baseline() -> None:
    # 15 runnable mutants at ~9s baseline each ≈ 2m 15s — the LOD-110 example.
    line = _progress_estimate(runnable=15, total=15, baseline_secs=9.0)
    assert line == "15 mutants; baseline ~9.0s each → estimated ≈ 2m 15s"


def test_progress_estimate_excludes_ignored_from_time_but_notes_them() -> None:
    # Ignored mutants never run, so they drop out of the ETA but are still counted and flagged.
    line = _progress_estimate(runnable=2, total=5, baseline_secs=10.0)
    assert line == "5 mutants (3 ignored, not run); baseline ~10.0s each → estimated ≈ 20s"


def test_progress_estimate_singular_for_one_mutant() -> None:
    assert _progress_estimate(runnable=1, total=1, baseline_secs=3.0).startswith("1 mutant;")


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
    # Multi-file (LOD-79): the baseline suite runs ONCE, then each file's mutants run in turn. A
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
