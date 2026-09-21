"""Tests for `scripts/benchmark.py`: that each scenario measures the work it says it does, and that
a comparison or a trend never reads as a pass when it could not check something.

Timings are never asserted. They depend on the machine, and a test that fails on a slow runner
teaches people to ignore it. What is asserted is the work: mutant counts, killed counts, restored
files, and the exit codes of `--compare`.

Loaded by path, like the other script tests, and registered in `sys.modules` first because the
script defines dataclasses, which look their own module up while being created.
"""

from __future__ import annotations

import collections
import functools
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from gdmutant.adapters.gdscript import is_valid_gdscript
from gdmutant.engine.operators import CATALOG

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "benchmark.py"
_spec = importlib.util.spec_from_file_location("benchmark", _SCRIPT)
assert _spec and _spec.loader
benchmark = importlib.util.module_from_spec(_spec)
sys.modules["benchmark"] = benchmark
_spec.loader.exec_module(benchmark)

CORPUS = benchmark.Workload("corpus", benchmark.CORPUS_FILE.read_text(encoding="utf-8"))


@functools.cache
def _corpus(scenario: str) -> Any:
    """One measurement per scenario for the whole module: each costs a real engine pass, and this
    file runs inside every mutation-sweep trial, so no test pays for the same one twice."""
    return benchmark.measure(scenario, CORPUS, repeat=1, warmup=False)


# --- the synthetic workload ---------------------------------------------------------------------


def test_the_synthetic_file_is_the_same_every_time_and_is_valid_gdscript() -> None:
    assert benchmark.synthetic_source(3) == benchmark.synthetic_source(3)
    assert benchmark.synthetic_source(3) != benchmark.synthetic_source(4)
    assert is_valid_gdscript(benchmark.synthetic_source(3))


def test_the_synthetic_file_exercises_every_operator() -> None:
    mutants = benchmark.generate_mutants("s.gd", benchmark.synthetic_source(2))
    seen = {m.operator_id for m in mutants}
    assert {op.id for op in CATALOG} <= seen
    assert "statement-deletion" in seen


def test_the_synthetic_node_paths_produce_no_arithmetic_mutants() -> None:
    # One real `/` and one real `%` per function. The `$Board/CellN` and `%HealthBar` on every
    # function must add nothing, so a count above this means node paths are being mutated again.
    functions = 3
    mutants = benchmark.generate_mutants("s.gd", benchmark.synthetic_source(functions))
    by_original = collections.Counter(m.original for m in mutants)
    assert by_original["/"] == functions  # `b / 3` -> `*`
    assert by_original["%"] == 2 * functions  # `a % 7` -> `*` and `/`


# --- the fake runner ----------------------------------------------------------------------------


def test_the_instant_runner_reads_the_project_dir_it_is_given(tmp_path: Path) -> None:
    # Under --jobs the engine mutates a worker's copy, not the original project. A runner that read
    # the original saw no mutation at all, so every parallel mutant survived: 0 killed where the
    # serial run killed 10. This pins that the copy it is handed is the one it reads.
    original = tmp_path / "original"
    copy = tmp_path / "copy"
    original.mkdir()
    copy.mkdir()
    (original / "f.gd").write_text("a", encoding="utf-8")
    runner = benchmark.InstantRunner({"f.gd": "a"})
    assert runner.run(str(original)).failures == 0
    mutated = next(t for t in ("b", "c", "d", "e") if benchmark.zlib.crc32(t.encode()) % 2 == 0)
    (copy / "f.gd").write_text(mutated, encoding="utf-8")
    assert runner.run(str(copy)).failures == 1


# --- the scenarios ------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", benchmark.ENGINE_SCENARIOS)
def test_each_engine_scenario_measures_the_corpus(scenario: str) -> None:
    result = _corpus(scenario)
    assert result.scenario == scenario
    assert result.mutants == 18  # the corpus's documented mutant count
    assert len(result.times) == 1 and result.times[0] >= 0
    if scenario in ("run", "run-jobs4", "report"):
        assert result.killed is not None and 0 < result.killed < result.mutants
    else:
        assert result.killed is None
    # Every corpus mutant is valid GDScript. `generate` does not apply mutants, so it has no count.
    assert result.invalid == (None if scenario == "generate" else 0)


def test_the_parallel_run_does_the_same_work_as_the_serial_run() -> None:
    serial, parallel = _corpus("run"), _corpus("run-jobs4")
    assert (parallel.mutants, parallel.killed) == (serial.mutants, serial.killed)


def test_the_multi_file_run_adds_up_to_its_files() -> None:
    # Two identical files under different names: the fake runner's verdict depends only on the
    # mutated text, so the multi-file totals must be exactly twice the single-file run's.
    twin = benchmark.Workload("twin", CORPUS.source)
    combined = benchmark.measure_run_files([CORPUS, twin], repeat=1, warmup=False)
    single = _corpus("run")
    assert combined.workload == "corpus+twin"
    assert (combined.mutants, combined.killed) == (2 * single.mutants, 2 * single.killed)


def test_an_unknown_engine_scenario_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown engine scenario"):
        benchmark.measure("nope", CORPUS, repeat=1, warmup=False)


def test_the_synthetic_workload_carries_exactly_one_rejected_mutant_per_function() -> None:
    """Without a mutant the re-parse gate must reject, every benchmark workload would be all valid,
    and a gate that accepted anything would produce the same numbers. `-float(a)` to `+float(a)` is
    the one rejection real code was found to produce, so each synthetic function carries it, and
    both the apply path and the full loop must count it."""
    small = benchmark.Workload("synthetic-1", benchmark.synthetic_source(1))
    applied = benchmark.measure("apply", small, repeat=1, warmup=False)
    ran = benchmark.measure("run", small, repeat=1, warmup=False)
    assert applied.invalid == ran.invalid == 1
    two = benchmark.Workload("synthetic-2", benchmark.synthetic_source(2))
    assert benchmark.measure("apply", two, repeat=1, warmup=False).invalid == 2


def test_compare_refuses_a_baseline_recorded_before_invalid_counts_existed() -> None:
    # An old baseline has no invalid count, so it cannot vouch for the gate: not comparable, not ok.
    old = _r()
    del old["invalid"]
    code, lines = benchmark.compare([_r()], [old], 0.25, 0.005)
    assert code == benchmark.EXIT_NOT_COMPARABLE
    assert lines[0].startswith("NOT COMPARABLE")


# --- compare ------------------------------------------------------------------------------------


def _r(
    scenario: str = "run",
    median: float = 1.0,
    mutants: int = 10,
    killed: int | None = 5,
    invalid: int | None = 1,
) -> Any:
    return {
        "scenario": scenario,
        "workload": "corpus",
        "mutants": mutants,
        "killed": killed,
        "invalid": invalid,
        "median": median,
    }


def test_compare_passes_within_the_tolerance() -> None:
    code, lines = benchmark.compare([_r(median=1.2)], [_r(median=1.0)], 0.25, 0.005)
    assert code == benchmark.EXIT_OK
    assert lines[0].startswith("ok")


def test_compare_fails_a_slowdown_past_the_tolerance() -> None:
    code, lines = benchmark.compare([_r(median=1.3)], [_r(median=1.0)], 0.25, 0.005)
    assert code == benchmark.EXIT_SLOWER
    assert lines[0].startswith("SLOWER")


def test_compare_ignores_a_large_ratio_on_a_tiny_absolute_change() -> None:
    # 0.001s to 0.002s is 2x, but it is timer noise, not a regression.
    code, _ = benchmark.compare([_r(median=0.002)], [_r(median=0.001)], 0.25, 0.005)
    assert code == benchmark.EXIT_OK


@pytest.mark.parametrize(
    ("current", "why"),
    [
        (_r(mutants=11), "a different mutant count"),
        (_r(killed=6), "a different killed count"),
        (_r(invalid=0), "a different invalid count: the re-parse gate answered differently"),
        (_r(scenario="apply"), "a scenario the baseline never measured"),
    ],
)
def test_compare_refuses_different_work(current: Any, why: str) -> None:
    code, lines = benchmark.compare([current], [_r()], 0.25, 0.005)
    assert code == benchmark.EXIT_NOT_COMPARABLE, why
    assert any(line.startswith("NOT COMPARABLE") for line in lines)


def test_compare_refuses_when_a_baseline_measurement_is_missing_now() -> None:
    code, lines = benchmark.compare([], [_r()], 0.25, 0.005)
    assert code == benchmark.EXIT_NOT_COMPARABLE
    assert "not measured now" in lines[0]


def test_not_comparable_outranks_slower() -> None:
    current = [_r(median=5.0), _r(scenario="apply", mutants=99)]
    baseline = [_r(median=1.0), _r(scenario="apply")]
    code, _ = benchmark.compare(current, baseline, 0.25, 0.005)
    assert code == benchmark.EXIT_NOT_COMPARABLE


# --- history and trend --------------------------------------------------------------------------


_ENV = {
    "python": "3.12.0",
    "platform": "p",
    "machine": "m",
    "processor_count": 8,
    "commit": "aaa",
}


def _run_line(
    commit: str, median: float, env: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    environment = {**(env or _ENV), "commit": commit}
    return {"environment": environment, "results": [_r(median=median, **kw)]}


def test_trend_says_when_there_is_no_history(tmp_path: Path) -> None:
    lines = benchmark.trend(tmp_path / "missing.jsonl", _ENV)
    assert "no history" in lines[0]


def test_trend_shows_each_recorded_median_and_the_overall_change(tmp_path: Path) -> None:
    history = tmp_path / "h.jsonl"
    benchmark.record(history, _run_line("aaa", 1.0))
    benchmark.record(history, _run_line("bbb", 1.5))
    lines = benchmark.trend(history, _ENV)
    assert lines[0] == "2 of 2 recorded run(s) match this machine and Python"
    assert "aaa:1.0000s" in lines[1] and "bbb:1.5000s" in lines[1]
    assert "+50%" in lines[1]


def test_trend_leaves_out_other_machines_and_says_so(tmp_path: Path) -> None:
    history = tmp_path / "h.jsonl"
    benchmark.record(history, _run_line("aaa", 1.0))
    benchmark.record(history, _run_line("bbb", 9.0, env={**_ENV, "processor_count": 64}))
    lines = benchmark.trend(history, _ENV)
    assert lines[0] == "1 of 2 recorded run(s) match this machine and Python"
    assert "bbb" not in "\n".join(lines)


def test_trend_flags_a_series_whose_work_changed(tmp_path: Path) -> None:
    history = tmp_path / "h.jsonl"
    benchmark.record(history, _run_line("aaa", 1.0))
    benchmark.record(history, _run_line("bbb", 1.0, mutants=12))
    assert "counts changed" in benchmark.trend(history, _ENV)[1]


def test_the_recorded_commit_says_when_the_tree_was_not_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = {"rev-parse": "abc1234", "status": " M gdmutant/cli.py"}
    monkeypatch.setattr(benchmark, "_git", lambda *args: answers[args[0]])
    assert benchmark.environment()["commit"] == "abc1234+dirty"
    answers["status"] = ""
    assert benchmark.environment()["commit"] == "abc1234"


def test_the_table_fits_a_long_workload_name() -> None:
    long = benchmark.Result("run-files", "corpus+synthetic-2+synthetic-8", 10, 5, 0, 1, [1.0])
    short = benchmark.Result("run", "corpus", 10, 5, 0, 1, [1.0])
    rows = benchmark._table([long, short]).splitlines()
    assert len({row.index(" 10 ") for row in rows[1:]}) == 1  # the mutant column lines up


# --- main ---------------------------------------------------------------------------------------


def test_main_writes_records_and_compares_against_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "run.json"
    history = tmp_path / "history.jsonl"
    args = ["--sizes", "1", "--repeat", "1", "--scenario", "generate"]
    assert benchmark.main([*args, "--json", str(out), "--record", str(history)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {r["workload"] for r in payload["results"]} == {"corpus", "synthetic-1"}
    assert payload["settings"]["scenarios"] == ["generate"]
    assert len(history.read_text(encoding="utf-8").splitlines()) == 1
    # Its own baseline can only come back ok or, on a noisy machine, slower. Never not comparable.
    assert benchmark.main([*args, "--compare", str(out)]) in (
        benchmark.EXIT_OK,
        benchmark.EXIT_SLOWER,
    )
    assert benchmark.main(["--trend", str(history)]) == 0
    assert "1 of 1 recorded run(s)" in capsys.readouterr().out


def test_main_refuses_the_godot_scenario_without_a_godot() -> None:
    with pytest.raises(SystemExit) as exc:
        benchmark.main(["--scenario", "godot-corpus"])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", [["--repeat", "0"], ["--sizes", "0"], ["--sizes", "x"]])
def test_main_rejects_bad_settings(bad: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        benchmark.main(bad)
    assert exc.value.code == 2
