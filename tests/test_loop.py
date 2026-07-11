"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import MarkerRunner

from gdmutant.engine.loop import BaselineFailed, Verdict, run
from gdmutant.engine.operators import TableOperator
from gdmutant.engine.runner import SuiteResult


@dataclass
class RaiseAfterBaselineRunner:
    """Passes the baseline (first call), then raises — to test the ERROR verdict + restore."""

    calls: int = 0

    def run(self, project_dir: str) -> SuiteResult:
        self.calls += 1
        if self.calls == 1:
            return SuiteResult(tests=1, failures=0, errors=0)
        raise RuntimeError("runner boom")


@dataclass
class ScriptedRunner:
    """Returns or raises per call, from a fixed script — for exercising mid-run failures."""

    script: list[SuiteResult | Exception]
    calls: int = 0

    def run(self, project_dir: str) -> SuiteResult:
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class ProjectDirRecordingRunner:
    """Records every project_dir it is handed (all-pass, so all mutants survive)."""

    seen: list[str] = field(default_factory=list)

    def run(self, project_dir: str) -> SuiteResult:
        self.seen.append(project_dir)
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
    # The marker is present in the ORIGINAL source, so the unmutated baseline "fails".
    with pytest.raises(BaselineFailed, match=r"the unmutated test suite failed"):
        run(str(tmp_path), path, src, MarkerRunner(target=path, kill_marker=">"))
    assert Path(path).read_text(encoding="utf-8") == src


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


def test_progress_callback_fires_once_per_mutant_in_order(tmp_path: Path) -> None:
    # Progress must fire once per mutant, in generation order, with the exact
    # "[i/N] path:line:col  a -> b  ... verdict" format — pinned exactly so a mutated separator,
    # index base, or verdict label is caught (LOD-86). Source has 3 mutants: >, and, <.
    src = "func f(a, b):\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    lines: list[str] = []
    runner = MarkerRunner(target=path, kill_marker=">=")
    run(str(tmp_path), path, src, runner, progress=lines.append)
    assert lines == [
        f"[1/3] {path}:2:11  > -> >=  ... killed",
        f"[2/3] {path}:2:15  and -> or  ... survived",
        f"[3/3] {path}:2:21  < -> <=  ... survived",
    ]


def test_progress_reports_invalid_verdict(tmp_path: Path) -> None:
    # An invalid (unparseable) mutant is still reported, labelled "invalid" — the progress line is
    # emitted for every mutant, not only the ones that reach the runner.
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    bad = TableOperator("bad", {">": ("))",)})
    lines: list[str] = []
    run(str(tmp_path), path, src, MarkerRunner(path, "ZZZ"), catalog=(bad,), progress=lines.append)
    assert lines == [f"[1/1] {path}:2:11  > -> ))  ... invalid"]


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
