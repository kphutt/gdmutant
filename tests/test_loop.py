"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import MarkerRunner

from gdmutant.engine.loop import BaselineFailed, Verdict, _derive_timeout, run
from gdmutant.engine.operators import TableOperator
from gdmutant.engine.runner import SuiteResult, SuiteTimeout


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


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_killed_survived_and_score(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"
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
    src = "func f(a, b):\n\treturn a > b\n"
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
    src = "func f(a):\n\treturn a > 5\n"
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
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    runner = ProjectDirRecordingRunner()
    run(str(tmp_path), path, src, runner)
    assert len(runner.seen) >= 2  # baseline + at least one mutant
    assert all(seen == str(tmp_path) for seen in runner.seen)


def test_baseline_failure_raises(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b\n"
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
    src = "func f(a, b):\n\treturn a > b\n"
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
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(BaselineFailed, match=r"could not run the unmutated suite"):
        run(str(tmp_path), path, src, ScriptedRunner([RuntimeError("godot missing")]))


def test_runner_exception_is_tallied_as_error_and_file_restored(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b\n"  # one mutant: '>'
    path = _write(tmp_path, "f.gd", src)
    result = run(str(tmp_path), path, src, RaiseAfterBaselineRunner())
    assert [o.verdict for o in result.outcomes] == [Verdict.ERROR]
    assert result.errors == 1 and result.mutation_score is None
    assert Path(path).read_text(encoding="utf-8") == src  # restored despite the runner error


def test_suite_timeout_is_tallied_as_timeout_and_counts_as_detected(tmp_path: Path) -> None:
    # A mutation that hangs the suite (runner raises SuiteTimeout) is a DETECTION, not an error:
    # tallied TIMEOUT, distinct from ERROR, and counted toward the score like a kill.
    src = "func f(a, b):\n\treturn a > b\n"  # one mutant: '>'
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
    src = "func f(a, b):\n\treturn a > b and a < b\n"  # 3 mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner)
    assert runner.seen[0] is None  # baseline uses the runner's configured budget
    assert runner.seen[1:] == [10.0, 10.0, 10.0]  # instant baseline -> derived floor of 10s


def test_explicit_timeout_overrides_derivation_for_each_mutant(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"  # 3 mutants
    path = _write(tmp_path, "f.gd", src)
    runner = TimeoutRecordingRunner()
    run(str(tmp_path), path, src, runner, timeout=25.0)
    assert runner.seen[0] is None  # baseline still uses the runner's own budget
    assert runner.seen[1:] == [25.0, 25.0, 25.0]  # explicit value, not derived


def test_runner_error_on_a_later_mutant_preserves_earlier_verdicts(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"  # 3 mutants: >, and, <
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
    src = "func f(a, b):\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    lines: list[str] = []
    runner = MarkerRunner(target=path, kill_marker=">=")
    run(str(tmp_path), path, src, runner, timeout=10.0, progress=lines.append)
    assert lines == [
        "running the unmutated (baseline) suite ...",
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
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    invalid: list[str] = []
    bad = TableOperator("bad", {">": ("))",)})
    run(
        str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=(bad,), progress=invalid.append
    )
    # An invalid mutant never runs, so it gets NO heartbeat — only the verdict line.
    assert invalid == [
        "running the unmutated (baseline) suite ...",
        f"[1/1] {path}:2:11  > -> ))  ... invalid",
    ]

    # An erroring mutant DID run, so it gets the heartbeat then the error verdict.
    errored: list[str] = []
    run(str(tmp_path), path, src, RaiseAfterBaselineRunner(), timeout=10.0, progress=errored.append)
    assert errored == [
        "running the unmutated (baseline) suite ...",
        f"[1/1] {path}:2:11  > -> >=  running (<=10s) ...",
        f"[1/1] {path}:2:11  > -> >=  ... error",
    ]


def test_progress_defaults_to_silent(tmp_path: Path) -> None:
    # Omitting progress must run without error and produce the same outcomes — it is opt-in.
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    result = run(str(tmp_path), path, src, MarkerRunner(path, ">="))
    assert [o.verdict for o in result.outcomes] == [Verdict.KILLED]


def test_run_is_deterministic(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    r1 = run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">="))
    r2 = run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">="))
    assert r1.outcomes == r2.outcomes
