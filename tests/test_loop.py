"""Tests for the mutation-run loop (no Godot — fake runners drive killed/survived)."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from gdmutant.engine.loop import BaselineFailed, Verdict, run
from gdmutant.engine.operators import TableOperator
from gdmutant.engine.runner import SuiteResult


@dataclass
class MarkerFakeRunner:
    """Simulates a test that 'catches' a mutation: the suite fails iff the target file contains
    `kill_marker`. Reads the file each call, so it reacts to whatever the loop wrote to disk."""

    target: str
    kill_marker: str
    tests: int = 3

    def run(self, project_dir: str) -> SuiteResult:
        content = Path(self.target).read_text(encoding="utf-8")
        return SuiteResult(tests=self.tests, failures=int(self.kill_marker in content), errors=0)


@dataclass
class RaiseAfterBaselineRunner:
    """Passes the baseline (first call), then raises — to test restore-on-exception."""

    calls: int = 0

    def run(self, project_dir: str) -> SuiteResult:
        self.calls += 1
        if self.calls == 1:
            return SuiteResult(tests=1, failures=0, errors=0)
        raise RuntimeError("runner boom")


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_killed_survived_and_score(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    # The "test" only catches the mutation that yields ">=", i.e. the '>' -> '>=' comparison mutant.
    result = run(str(tmp_path), path, src, MarkerFakeRunner(target=path, kill_marker=">="))

    assert (result.killed, result.survived, result.invalid) == (1, 2, 0)
    assert result.mutation_score == pytest.approx(1 / 3)
    (killed,) = [o.mutant for o in result.outcomes if o.verdict is Verdict.KILLED]
    assert (killed.original, killed.replacement) == (">", ">=")
    assert {(m.original, m.replacement) for m in result.survivors} == {("<", "<="), ("and", "or")}
    assert Path(path).read_text(encoding="utf-8") == src  # restored


def test_invalid_mutant_is_nf5_classified(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    bad = TableOperator("bad", {">": ("))",)})  # produces unparseable GDScript
    result = run(str(tmp_path), path, src, MarkerFakeRunner(path, "ZZZ"), catalog=(bad,))

    assert [o.verdict for o in result.outcomes] == [Verdict.INVALID]
    assert (result.killed, result.survived, result.invalid) == (0, 0, 1)
    assert result.mutation_score is None  # no killable mutants
    assert Path(path).read_text(encoding="utf-8") == src  # invalid short-circuits before any write


def test_baseline_failure_raises(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    # The marker is present in the ORIGINAL source, so the unmutated baseline "fails".
    with pytest.raises(BaselineFailed):
        run(str(tmp_path), path, src, MarkerFakeRunner(target=path, kill_marker=">"))
    assert Path(path).read_text(encoding="utf-8") == src


def test_restores_file_on_runner_exception(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b\n"
    path = _write(tmp_path, "f.gd", src)
    with pytest.raises(RuntimeError, match="boom"):
        run(str(tmp_path), path, src, RaiseAfterBaselineRunner())
    assert Path(path).read_text(encoding="utf-8") == src  # restored despite the exception


def test_run_is_deterministic(tmp_path: Path) -> None:
    src = "func f(a, b):\n\treturn a > b and a < b\n"
    path = _write(tmp_path, "f.gd", src)
    r1 = run(str(tmp_path), path, src, MarkerFakeRunner(target=path, kill_marker=">="))
    r2 = run(str(tmp_path), path, src, MarkerFakeRunner(target=path, kill_marker=">="))
    assert r1.outcomes == r2.outcomes
