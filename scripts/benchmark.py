"""Performance benchmark for gdmutant's own engine, with a size sweep and a recorded history, so
the cost of gdmutant itself can be watched as a trend over time.

What kind of test this is. Not a load test in the usual sense: that means many users or requests
hitting a running service at once, and gdmutant is a command-line tool that runs one batch. It is
three related things instead:

  a benchmark          the same fixed work, timed repeatably
  a scalability test   the same work at growing sizes, to show how cost grows with the input
  a regression check   today's numbers against a saved baseline, and every run kept as history

What it measures. A real run spends most of its time inside Godot, running the user's suite once
per mutant. That is the user's cost and differs per project. gdmutant's own cost is everything
around it: parsing, finding sites, re-parsing each mutant to reject invalid GDScript, writing and
restoring files, copying projects for `--jobs`, classifying results, and rendering reports. The
engine scenarios drive the real engine with an instant fake runner and no Godot, so that part is
measured on its own. The opt-in `godot-corpus` scenario is the realistic end to end run.

Scenarios:

  generate      parse the file and list every mutant (`generate_mutants`)
  apply         apply every mutant and re-parse the result (`apply_mutant`, the validity check)
  run           the full engine loop (`engine.loop.run`), serially, with an instant fake runner
  run-jobs4     the same loop with `jobs=4`, including the per-worker project copies
  run-files     one multi-file run (`engine.loop.run_paths`) over every workload at once, the way a
                directory run works
  report        render the JSON and HTML reports for a finished run
  godot-corpus  the real engine and a real Godot, via the corpus's own test harness. Opt in with
                `--godot PATH`. Slow: Godot starts once per mutant.

Workloads:

  corpus        `corpus/turn_order.gd`, the real fixture the live self-tests use
  synthetic-N   a generated file with N functions using every operator. Deterministic, so the same
                N is the same file on every machine and every run.

The fake runner never launches a process. It reads the files the engine just wrote in the project
directory it is handed (a worker's copy under `--jobs`) and fails the suite for a fixed half of the
mutants, chosen by a checksum of the mutated file. So the loop takes both its killed and its
survived paths, the same way on every run and at every worker count.

Usage, from the repo root:

  uv run python scripts/benchmark.py                                 # print a table
  uv run python scripts/benchmark.py --record                        # also add it to the history
  uv run python scripts/benchmark.py --trend                         # show the history as trends
  uv run python scripts/benchmark.py --sizes 2,8,32 --json before.json
  uv run python scripts/benchmark.py --json after.json --compare before.json

The history defaults to `.benchmarks/history.jsonl`, which git ignores, because timings belong to
one machine. `--trend` only shows runs recorded on a machine and Python matching this one.

`--compare` exits 1 when a scenario's median got slower than the baseline's by more than
`--tolerance` (default 25%) and by more than `--min-delta` seconds (default 0.005). It exits 2 when
the two runs did not do the same work: a scenario or workload missing from either side, or a
different mutant or killed count. A comparison that could not check something never passes.
See docs/benchmarking.md for how to get numbers worth comparing.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:  # so `python scripts/benchmark.py` imports this checkout's gdmutant
    sys.path.insert(0, str(REPO))

from gdmutant.adapters.gdscript import ADAPTER, apply_mutant, generate_mutants  # noqa: E402
from gdmutant.engine.loop import MutationRun, run, run_paths  # noqa: E402
from gdmutant.engine.report import html_report, stryker_report  # noqa: E402
from gdmutant.engine.runner import CommandRunner, SuiteResult  # noqa: E402

CORPUS_DIR = REPO / "corpus"
CORPUS_FILE = CORPUS_DIR / "turn_order.gd"
DEFAULT_SIZES = (2, 8)
DEFAULT_REPEAT = 3
DEFAULT_TOLERANCE = 0.25
DEFAULT_MIN_DELTA = 0.005
DEFAULT_HISTORY = REPO / ".benchmarks" / "history.jsonl"
ENGINE_SCENARIOS = ("generate", "apply", "run", "run-jobs4", "report")
SCENARIOS = (*ENGINE_SCENARIOS, "run-files", "godot-corpus")

#: Exit codes for `--compare`.
EXIT_OK = 0
EXIT_SLOWER = 1
EXIT_NOT_COMPARABLE = 2

#: The environment fields a trend is split by. Numbers from a different machine or Python are a
#: different series, not a change in gdmutant.
_SERIES_KEYS = ("python", "platform", "machine", "processor_count")


def synthetic_source(functions: int) -> str:
    """A GDScript file with `functions` functions, each using every kind of site gdmutant mutates:
    comparisons, `and`/`or`/`not`, arithmetic, `%`, compound assignment, `true`/`false`, integer,
    float and hex literals, and deletable statements. Each function also holds a node path and a
    unique node name, which must produce no arithmetic mutants, so a regression that starts
    mutating them again changes the mutant count and makes `--compare` refuse.

    Deterministic: the same `functions` gives the same text, byte for byte."""
    lines = ["extends Node", ""]
    for i in range(functions):
        lines += [
            f"func step_{i}(a: int, b: int, ready: bool) -> float:",
            f"\tvar total := 0.5 + {i}",
            "\tif a > b and not ready:",
            "\t\ttotal += a * 2",
            "\telif a <= b or ready:",
            "\t\ttotal -= b / 3",
            "\tvar ratio := (a % 7) * 1.25",
            "\tvar mask := 0xFF",
            f"\tvar node := $Board/Cell{i}",
            "\tvar bar := %HealthBar",
            "\tvar done := false",
            "\tprint(node, bar, mask, done)",
            "\treturn total * ratio if a != b else 0.0",
            "",
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class Workload:
    """A named GDScript source to benchmark against."""

    name: str
    source: str

    @property
    def filename(self) -> str:
        return f"{self.name}.gd"


def workloads(sizes: tuple[int, ...]) -> list[Workload]:
    """The corpus file plus one synthetic file per size, smallest first."""
    found = [Workload("corpus", CORPUS_FILE.read_text(encoding="utf-8"))]
    return found + [Workload(f"synthetic-{n}", synthetic_source(n)) for n in sorted(sizes)]


class InstantRunner:
    """A test runner that never launches anything.

    It reads the named files inside the `project_dir` it is given, which under `--jobs` is a
    worker's own copy, not the original project. When every file matches its original, the suite
    passes. When one differs, the suite fails if the CRC32 of that file's text is even, which splits
    mutants roughly in half between killed and survived, identically everywhere."""

    def __init__(self, originals: dict[str, str]) -> None:
        self.originals = originals

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        """Pass or fail the files as the engine left them, by the rule above."""
        for name, original in self.originals.items():
            text = (Path(project_dir) / name).read_text(encoding="utf-8")
            if text != original:
                failed = zlib.crc32(text.encode("utf-8")) % 2 == 0
                return SuiteResult(tests=1, failures=int(failed), errors=0)
        return SuiteResult(tests=1, failures=0, errors=0)


@dataclass
class Result:
    """One scenario timed on one workload."""

    scenario: str
    workload: str
    mutants: int
    killed: int | None
    repeats: int
    times: list[float]

    @property
    def minimum(self) -> float:
        return min(self.times)

    @property
    def median(self) -> float:
        return statistics.median(self.times)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "min": self.minimum, "median": self.median}


def _timed(action: Callable[[], Any], repeat: int, warmup: bool = True) -> tuple[list[float], Any]:
    """Run `action` once untimed as a warm-up, then `repeat` timed times. Returns the times and
    the last result. `warmup=False` is for the tests, which check the work done, not the time."""
    last = action() if warmup else None
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        last = action()
        times.append(time.perf_counter() - start)
    return times, last


def _new_project(tmp: Path, files: list[Workload]) -> Path:
    project = tmp / "project"
    project.mkdir()
    (project / "project.godot").write_text("", encoding="utf-8")
    for w in files:
        (project / w.filename).write_text(w.source, encoding="utf-8")
    return project


def _check_restored(project: Path, files: list[Workload], scenario: str) -> None:
    for w in files:
        if (project / w.filename).read_text(encoding="utf-8") != w.source:
            raise RuntimeError(f"{scenario} left {w.filename} mutated")


def measure(scenario: str, workload: Workload, repeat: int, warmup: bool = True) -> Result:
    """Time one of `ENGINE_SCENARIOS` on `workload`. Every result carries the mutant count, and the
    loop scenarios the killed count, so two runs can be checked for doing the same work before
    their times are compared."""
    source = workload.source
    mutants = generate_mutants(workload.filename, source)
    if scenario == "generate":
        times, _ = _timed(lambda: generate_mutants(workload.filename, source), repeat, warmup)
        return Result(scenario, workload.name, len(mutants), None, repeat, times)
    if scenario == "apply":
        times, _ = _timed(lambda: [apply_mutant(m, source) for m in mutants], repeat, warmup)
        return Result(scenario, workload.name, len(mutants), None, repeat, times)
    if scenario not in ("run", "run-jobs4", "report"):
        raise ValueError(f"unknown engine scenario {scenario!r}")
    with tempfile.TemporaryDirectory(prefix="gdmutant-bench-") as tmp:
        project = _new_project(Path(tmp), [workload])
        target = str(project / workload.filename)
        runner = InstantRunner({workload.filename: source})
        jobs = 4 if scenario == "run-jobs4" else 1

        def engine() -> MutationRun:
            return run(str(project), target, source, runner, ADAPTER, jobs=jobs)

        if scenario == "report":
            finished = engine()

            def render() -> str:
                report = stryker_report(finished, target, source, "gdscript")
                return json.dumps(report) + html_report(report, str(project))

            times, _ = _timed(render, repeat, warmup)
        else:
            times, finished = _timed(engine, repeat, warmup)
            _check_restored(project, [workload], scenario)
        return Result(scenario, workload.name, len(mutants), finished.killed, repeat, times)


def measure_run_files(files: list[Workload], repeat: int, warmup: bool = True) -> Result:
    """One multi-file run over every workload in a single project: one baseline, then each file's
    mutants in turn, the way `gdmutant run <directory>` works."""
    with tempfile.TemporaryDirectory(prefix="gdmutant-bench-") as tmp:
        project = _new_project(Path(tmp), files)
        sources = {str(project / w.filename): w.source for w in files}
        runner = InstantRunner({w.filename: w.source for w in files})
        times, finished = _timed(
            lambda: run_paths(str(project), sources, runner, ADAPTER), repeat, warmup
        )
        _check_restored(project, files, "run-files")
    mutants = sum(len(generate_mutants(w.filename, w.source)) for w in files)
    killed = sum(r.killed for r in finished.values())
    name = "+".join(w.name for w in files)
    return Result("run-files", name, mutants, killed, repeat, times)


def measure_godot_corpus(godot: str, repeat: int) -> Result:
    """The realistic run: the real engine and a real Godot on a copy of the corpus, through the
    corpus's own exit-code harness. Godot's import cache is built by the untimed warm-up run."""
    source = CORPUS_FILE.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="gdmutant-bench-godot-") as tmp:
        project = Path(tmp) / "corpus"
        shutil.copytree(
            CORPUS_DIR, project, ignore=shutil.ignore_patterns(".godot", "reports", "addons")
        )
        target = str(project / CORPUS_FILE.name)
        runner = CommandRunner(
            command=[godot, "--headless", "--path", ".", "--script", "res://harness/run_tests.gd"]
        )
        times, finished = _timed(lambda: run(str(project), target, source, runner, ADAPTER), repeat)
    mutants = len(generate_mutants(CORPUS_FILE.name, source))
    return Result("godot-corpus", "corpus", mutants, finished.killed, repeat, times)


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def environment() -> dict[str, Any]:
    """What a number depends on, recorded beside it.

    The commit is marked `+dirty` when tracked files have uncommitted changes, because then the
    number belongs to code no commit holds, and a trend point labelled with a clean commit would
    claim otherwise."""
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    changes = _git("status", "--porcelain", "--untracked-files=no")
    if changes:
        commit += "+dirty"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor_count": os.cpu_count(),
        "commit": commit,
    }


def compare(
    current: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    tolerance: float,
    min_delta: float,
) -> tuple[int, list[str]]:
    """Compare two result lists. Returns an exit code and the lines explaining it.

    Not comparable (exit 2) wins over slower (exit 1): when the two runs did different work, a time
    difference means nothing, and a missing measurement must never read as a pass."""
    lines: list[str] = []
    code = EXIT_OK
    base = {(r["scenario"], r["workload"]): r for r in baseline}
    seen = set()
    for r in current:
        key = (r["scenario"], r["workload"])
        seen.add(key)
        old = base.get(key)
        label = f"{key[0]} on {key[1]}"
        if old is None:
            lines.append(f"NOT COMPARABLE  {label}: not in the baseline")
            code = EXIT_NOT_COMPARABLE
            continue
        if (old["mutants"], old["killed"]) != (r["mutants"], r["killed"]):
            lines.append(
                f"NOT COMPARABLE  {label}: {r['mutants']} mutants and {r['killed']} killed now, "
                f"{old['mutants']} and {old['killed']} in the baseline, so this is different work"
            )
            code = EXIT_NOT_COMPARABLE
            continue
        delta = r["median"] - old["median"]
        ratio = r["median"] / old["median"] if old["median"] > 0 else float("inf")
        verdict = "ok"
        if delta > min_delta and ratio > 1 + tolerance:
            verdict = "SLOWER"
            if code == EXIT_OK:
                code = EXIT_SLOWER
        lines.append(
            f"{verdict:15} {label}: median {old['median']:.4f}s -> {r['median']:.4f}s "
            f"({ratio:.2f}x)"
        )
    for key in sorted(set(base) - seen):
        lines.append(f"NOT COMPARABLE  {key[0]} on {key[1]}: in the baseline, not measured now")
        code = EXIT_NOT_COMPARABLE
    return code, lines


def record(history: Path, payload: dict[str, Any]) -> None:
    """Append one run to the history file, one JSON object per line."""
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def trend(history: Path, current_environment: dict[str, Any]) -> list[str]:
    """The history as one row per scenario and workload: each recorded median in order, oldest
    first, labelled by commit, and the change from the first to the last. Only runs from a machine
    and Python matching `current_environment` are included, and the lines say how many were left
    out, so a quiet history is never mistaken for a flat one."""
    if not history.is_file():
        return [f"no history at {history}. Record a run with --record."]
    runs = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line]
    series = {k: current_environment.get(k) for k in _SERIES_KEYS}
    matching = [r for r in runs if {k: r["environment"].get(k) for k in _SERIES_KEYS} == series]
    lines = [f"{len(matching)} of {len(runs)} recorded run(s) match this machine and Python"]
    rows: dict[tuple[str, str], list[tuple[str, float, int, int | None]]] = {}
    for r in matching:
        for res in r["results"]:
            key = (res["scenario"], res["workload"])
            rows.setdefault(key, []).append(
                (r["environment"]["commit"], res["median"], res["mutants"], res["killed"])
            )
    for (scenario, workload), points in sorted(rows.items()):
        shown = "  ".join(f"{commit}:{median:.4f}s" for commit, median, _, _ in points)
        first, last = points[0][1], points[-1][1]
        change = f"{(last / first - 1) * 100:+.0f}%" if first > 0 else "n/a"
        work = {(m, k) for _, _, m, k in points}
        note = "" if len(work) == 1 else "  (mutant or killed counts changed along the way)"
        lines.append(f"{scenario:12} {workload:22} {change:>6}  {shown}{note}")
    return lines


def _table(results: list[Result]) -> str:
    width = max([len("workload"), *(len(r.workload) for r in results)])
    rows = [
        f"{'scenario':12} {'workload':{width}} {'mutants':>7} {'killed':>6} {'min':>9} "
        f"{'median':>9} {'per mutant':>11}"
    ]
    for r in results:
        per = r.median / r.mutants if r.mutants else 0.0
        killed = "-" if r.killed is None else str(r.killed)
        rows.append(
            f"{r.scenario:12} {r.workload:{width}} {r.mutants:>7} {killed:>6} {r.minimum:>8.4f}s "
            f"{r.median:>8.4f}s {per * 1000:>9.3f}ms"
        )
    return "\n".join(rows)


def _sizes(text: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--sizes takes comma-separated integers: {text!r}"
        ) from exc
    if not sizes or any(n < 1 for n in sizes):
        raise argparse.ArgumentTypeError(f"--sizes needs integers of 1 or more: {text!r}")
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sizes", type=_sizes, default=DEFAULT_SIZES, help="e.g. 2,8,32")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="timed runs per result")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="repeatable")
    parser.add_argument("--godot", help="a Godot binary. Enables the godot-corpus scenario.")
    parser.add_argument("--json", type=Path, help="write the results and environment here")
    parser.add_argument("--compare", type=Path, help="a --json file to compare this run with")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--min-delta", type=float, default=DEFAULT_MIN_DELTA)
    parser.add_argument(
        "--record", type=Path, nargs="?", const=DEFAULT_HISTORY, help="append to the history"
    )
    parser.add_argument(
        "--trend",
        type=Path,
        nargs="?",
        const=DEFAULT_HISTORY,
        help="print the history, run nothing",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    if args.trend is not None:
        print("\n".join(trend(args.trend, environment())))
        return EXIT_OK

    chosen = args.scenario or [s for s in SCENARIOS if s != "godot-corpus" or args.godot]
    if "godot-corpus" in chosen and not args.godot:
        parser.error("the godot-corpus scenario needs --godot PATH")
    files = workloads(args.sizes)
    results = [
        measure(scenario, workload, args.repeat)
        for workload in files
        for scenario in chosen
        if scenario in ENGINE_SCENARIOS
    ]
    if "run-files" in chosen:
        results.append(measure_run_files(files, args.repeat))
    if "godot-corpus" in chosen:
        results.append(measure_godot_corpus(args.godot, args.repeat))
    print(_table(results))

    payload: dict[str, Any] = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(),
        "settings": {"sizes": list(args.sizes), "repeat": args.repeat, "scenarios": chosen},
        "results": [r.as_dict() for r in results],
    }
    if args.json is not None:
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    if args.record is not None:
        record(args.record, payload)
        print(f"recorded in {args.record}")
    if args.compare is None:
        return EXIT_OK
    baseline = json.loads(args.compare.read_text(encoding="utf-8"))
    code, lines = compare(payload["results"], baseline["results"], args.tolerance, args.min_delta)
    base_env = baseline.get("environment", {})
    if any(base_env.get(k) != payload["environment"].get(k) for k in _SERIES_KEYS):
        lines.insert(0, "note: the baseline was recorded on a different machine or Python")
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
