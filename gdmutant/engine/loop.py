"""The mutation-run loop: select -> mutate -> run -> tally -> score.

For each mutant the adapter generates, the loop materializes it on disk, runs the target's test
suite via a `Runner`, and classifies the outcome:

* **killed** — the suite failed (a test caught the mutation),
* **survived** — the suite passed (no test caught it),
* **timeout** — the suite exceeded its time budget: a mutation-induced hang (infinite loop) *is* a
  detection, so it counts as killed (Stryker's ``Timeout`` status),
* **ignored** — a ``# gdmutant: ignore`` annotation suppresses it (an equivalent/unkillable mutant);
  generated for the report but never run, and excluded from the score (Stryker's ``Ignored``),
* **invalid** — the mutant didn't parse (NF-5); never counted as killed, never run,
* **error** — the runner failed to execute the mutant (e.g. a Godot crash); tallied so one bad run
  doesn't discard the whole pass, and excluded from the score.

no-coverage is folded into *survived* for v0.1 (DESIGN.md FG-4.1) until coverage-gating lands. Each
mutant is applied in isolation and the original restored in a ``finally`` — see docs/decisions/0003.
"""

from __future__ import annotations

import os
import queue
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gdmutant.engine.adapter import Adapter
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import CATALOG, Operator
from gdmutant.engine.runner import Preparable, Runner, SuiteTimeout

# Per-mutant timeout derived from the baseline's wall-clock, so a hanging mutant is cut off in
# seconds rather than blocking for a flat default (the #1 first-run "looks frozen" complaint).
_MIN_TIMEOUT = 10.0  # floor: never rule a mutant a hang in under 10s (absorbs jitter/cold caches)
_TIMEOUT_FACTOR = 10.0  # a mutant gets 10x the baseline's time before it's a hang
_MAX_DERIVED_TIMEOUT = 600.0  # cap: a slow suite is never worse off than the historical default


def _derive_timeout(baseline_secs: float) -> float:
    """Per-mutant timeout from the baseline run time: ``baseline * factor``, floored and capped."""
    return min(_MAX_DERIVED_TIMEOUT, max(_MIN_TIMEOUT, baseline_secs * _TIMEOUT_FACTOR))


class BaselineFailed(Exception):
    """The unmutated suite failed — mutation testing a red suite is meaningless (FG-3.3)."""


class Verdict(Enum):
    KILLED = "killed"
    SURVIVED = "survived"
    TIMEOUT = "timeout"
    IGNORED = "ignored"
    INVALID = "invalid"
    ERROR = "error"


@dataclass(frozen=True)
class MutantOutcome:
    mutant: Mutant
    verdict: Verdict


@dataclass(frozen=True)
class MutationRun:
    """The tally of a mutation run — outcomes in the order mutants were generated (NF-1)."""

    outcomes: tuple[MutantOutcome, ...]

    def _count(self, verdict: Verdict) -> int:
        return sum(1 for o in self.outcomes if o.verdict is verdict)

    @property
    def killed(self) -> int:
        return self._count(Verdict.KILLED)

    @property
    def survived(self) -> int:
        return self._count(Verdict.SURVIVED)

    @property
    def timeouts(self) -> int:
        return self._count(Verdict.TIMEOUT)

    @property
    def ignored(self) -> int:
        return self._count(Verdict.IGNORED)

    @property
    def invalid(self) -> int:
        return self._count(Verdict.INVALID)

    @property
    def errors(self) -> int:
        return self._count(Verdict.ERROR)

    @property
    def survivors(self) -> tuple[Mutant, ...]:
        """The mutants no test caught — the product's core output."""
        return tuple(o.mutant for o in self.outcomes if o.verdict is Verdict.SURVIVED)

    @property
    def detected(self) -> int:
        """Mutants a test caught — killed outright *or* by hanging the suite (timeout)."""
        return self.killed + self.timeouts

    @property
    def mutation_score(self) -> float | None:
        """``detected / (detected + survived)`` where detected = killed + timeouts; ``None`` when
        there are no killable mutants. Timeouts count as detected (Stryker convention): a mutation
        that hangs the suite was observably caught. invalid/error are excluded entirely."""
        scored = self.detected + self.survived
        return self.detected / scored if scored else None


def _progress_start(index: int, total: int, mutant: Mutant, timeout: float) -> str:
    """Heartbeat emitted *before* a mutant runs: shows which mutant is running and its time budget,
    so a slow or hanging mutant reads as "running mutant N (<=Ns)" rather than a frozen terminal —
    the #1 first-run complaint. The verdict line follows once it resolves.
    """
    loc = f"{mutant.path}:{mutant.span.line}:{mutant.span.column}"
    head = f"[{index}/{total}] {loc}  {mutant.describe_change()}"
    return f"{head}  running (<={timeout:g}s) ..."


def _format_duration(secs: float) -> str:
    """A compact human duration: ``9s``, ``2m 15s``, ``1h 3m``. Rounds to whole seconds."""
    whole = int(round(secs))
    if whole < 60:
        return f"{whole}s"
    minutes, s = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {s}s"
    hours, m = divmod(minutes, 60)
    return f"{hours}h {m}m"


def _progress_estimate(runnable: int, total: int, baseline_secs: float) -> str:
    """The pre-run estimate line: how many mutants will run and roughly how long, derived from the
    baseline's own wall-clock. "Looks hung" is the #1 documented reason people abandon mutation
    testing (LOD-110); a stated ETA makes a long run read as *expected*. Rough by construction —
    each mutant is budgeted at ~the baseline time, and killed mutants often finish sooner — so it's
    an upper-ish "about" figure, not a promise. Ignored mutants never run, so they're excluded from
    the time but noted in the count."""
    est = _format_duration(runnable * baseline_secs)
    ignored = total - runnable
    ignored_note = f" ({ignored} ignored, not run)" if ignored else ""
    unit = "mutant" if total == 1 else "mutants"
    return f"{total} {unit}{ignored_note}; baseline ~{baseline_secs:.1f}s each → estimated ≈ {est}"


def _progress_line(index: int, total: int, outcome: MutantOutcome) -> str:
    """One human-readable progress line for a finished mutant.

    Format: ``[i/N] path:line:col  original -> replacement  ... verdict`` — which mutant just
    resolved and how. The engine formats it; the caller (the CLI) decides where it goes — always
    stderr, so ``--json -`` keeps stdout pure JSON.
    """
    m = outcome.mutant
    loc = f"{m.path}:{m.span.line}:{m.span.column}"
    return f"[{index}/{total}] {loc}  {m.describe_change()}  ... {outcome.verdict.value}"


def run(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    catalog: tuple[Operator, ...] = CATALOG,
    *,
    timeout: float | None = None,
    progress: Callable[[str], None] | None = None,
    jobs: int = 1,
) -> MutationRun:
    """Run the full mutation pass over `source` (the contents of `path`) using `runner`.

    `adapter` supplies the two language-specific operations (generation + application), so the
    engine stays language-neutral (NF-3) — the caller injects it, e.g. `adapters.gdscript.ADAPTER`.

    Raises `BaselineFailed` if the unmutated suite doesn't pass first (FG-3.3). The file at `path`
    must hold `source` when this is called; it is restored to `source` before returning.

    `timeout` is the per-mutant time budget in seconds. When ``None`` (the default), it is *derived
    from the baseline run's own wall-clock* (`_derive_timeout`), so a hanging mutant is cut off in
    seconds instead of blocking for the flat default — an explicit value overrides the derivation.

    `jobs` is the number of mutants to evaluate concurrently (default 1 = serial). With ``jobs > 1``
    the loop gives each worker its own copy of the project so in-place mutation can't collide, then
    reassembles the outcomes in generation order (ADR-0003). Process isolation makes the pass/fail
    verdict of each mutant identical to a serial run; to keep the *timeout* verdict identical too,
    the per-mutant time budget is scaled by the worker count so CPU/RAM contention can't turn a
    genuinely-passing suite into a false TIMEOUT (see `_run_mutants_parallel`).

    If `progress` is given, it is called with the baseline notice, then — for each mutant that
    actually runs — a "running (<=Ns)" heartbeat *before* it runs and a verdict line *after*, in
    generation order (invalid mutants never run, so they get only the verdict line). The CLI wires
    it to stderr so a long or hanging run shows steady output instead of looking frozen; `None`
    runs silently. Under ``jobs > 1`` verdict lines arrive as each mutant finishes (their true
    generation index is shown); the per-mutant heartbeat is omitted (the lines would interleave).
    """
    per_mutant_timeout, baseline_secs = _run_baseline(project_dir, runner, timeout, progress)
    return _mutate_file(
        project_dir,
        path,
        source,
        runner,
        adapter,
        per_mutant_timeout,
        baseline_secs,
        catalog,
        progress,
        jobs,
    )


def _run_baseline(
    project_dir: str,
    runner: Runner,
    timeout: float | None,
    progress: Callable[[str], None] | None,
) -> tuple[float, float]:
    """Run the unmutated suite once. Returns ``(per_mutant_timeout, baseline_secs)`` — the
    per-mutant budget (derived from the baseline's wall-clock unless `timeout` overrides) and it.
    Raises `BaselineFailed` if the suite can't run or is red (FG-3.3)."""
    # One-time setup (e.g. a Godot import scan) runs BEFORE the clock starts, so its cost never
    # inflates the baseline wall-clock that derives per-mutant timeouts and the ETA (LOD-213). A
    # runner with nothing to prepare simply isn't Preparable — the engine stays language-neutral.
    if isinstance(runner, Preparable):
        if progress is not None:
            progress("preparing the project (one-time) ...")
        try:
            runner.prepare(project_dir)
        except Exception as error:  # a runner that can't even prepare is a setup error
            raise BaselineFailed(f"could not prepare {project_dir!r}: {error}") from error
    if progress is not None:
        progress("running the unmutated (baseline) suite ...")
    started = time.monotonic()
    try:
        baseline = runner.run(project_dir)
    except Exception as error:  # a runner that can't even run the unmutated suite is a setup error
        raise BaselineFailed(
            f"could not run the unmutated suite for {project_dir!r}: {error}"
        ) from error
    baseline_secs = time.monotonic() - started
    if baseline.failed:
        detail = f":\n{baseline.detail}" if baseline.detail else ""
        raise BaselineFailed(f"the unmutated test suite failed for {project_dir!r}{detail}")
    per_mutant_timeout = timeout if timeout is not None else _derive_timeout(baseline_secs)
    return per_mutant_timeout, baseline_secs


def _mutate_file(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    per_mutant_timeout: float,
    baseline_secs: float,
    catalog: tuple[Operator, ...],
    progress: Callable[[str], None] | None,
    jobs: int = 1,
) -> MutationRun:
    """Generate and run every mutant for a single file (the baseline is assumed already green). The
    file at `path` must hold `source`; it is restored before returning. `jobs > 1` evaluates mutants
    concurrently on per-worker project copies, then reassembles them in generation order."""
    mutants = adapter.generate_mutants(path, source, catalog)
    total = len(mutants)
    if progress is not None:
        runnable = sum(1 for m in mutants if m.ignore_reason is None)
        progress(_progress_estimate(runnable, total, baseline_secs))
    if jobs > 1 and total > 0:
        outcomes = _run_mutants_parallel(
            project_dir, path, source, runner, adapter, mutants, per_mutant_timeout, progress, jobs
        )
    else:
        outcomes = _run_mutants_serial(
            project_dir, path, source, runner, adapter, mutants, per_mutant_timeout, progress
        )
    return MutationRun(tuple(outcomes))


def _run_mutants_serial(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    mutants: Sequence[Mutant],
    per_mutant_timeout: float,
    progress: Callable[[str], None] | None,
) -> list[MutantOutcome]:
    """Evaluate every mutant one at a time against the real project file (the trusted default)."""
    outcomes: list[MutantOutcome] = []
    total = len(mutants)
    for index, mutant in enumerate(mutants, start=1):
        if mutant.ignore_reason is not None:
            # A `# gdmutant: ignore` annotation suppresses this mutant — generated for the report
            # but never run (no validity check, no suite run): tallied IGNORED, excluded from score.
            verdict = Verdict.IGNORED
        else:
            mutated, valid = adapter.apply_mutant(mutant, source)
            if valid:
                if progress is not None:
                    progress(_progress_start(index, total, mutant, per_mutant_timeout))
                verdict = _run_one(project_dir, path, source, mutated, runner, per_mutant_timeout)
            else:
                verdict = Verdict.INVALID
        outcome = MutantOutcome(mutant, verdict)
        outcomes.append(outcome)
        if progress is not None:
            progress(_progress_line(index, total, outcome))
    return outcomes


def _run_mutants_parallel(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    mutants: Sequence[Mutant],
    per_mutant_timeout: float,
    progress: Callable[[str], None] | None,
    jobs: int,
) -> list[MutantOutcome]:
    """Evaluate mutants concurrently, each worker on its OWN copy of the project so the in-place
    file mutation (`_run_one`) can never collide. The pass/fail/timeout verdict of each mutant is
    identical to the serial path — only the wall-clock changes — and results are reassembled in
    generation order (NF-1). Bounded to one worker per runnable mutant.

    Two things stay SINGLE-THREADED, up front, because they aren't parallel-safe or need no Godot:
    deciding `ignore`d/invalid mutants (no suite run), and **applying** each mutant — `apply_mutant`
    re-parses via gdtoolkit, whose lexer/indenter keeps shared state and is NOT thread-safe (running
    it from N workers corrupts it, e.g. a `paren_level` assertion deep in the parser). So the
    parse/mutate/validate half runs here on the main thread; workers do only the process-isolated
    test run, which touches no shared parser. Applying is cheap; booting Godot is the cost — so this
    keeps the expensive half parallel while making the whole thing sound.

    Runners are stateless (keyed on `project_dir` per call), so the one instance is shared across
    workers; each passes its own copy's dir. The Godot import cache (`.godot/`) is copied along with
    the project, so a worker doesn't pay a cold re-scan.

    The per-mutant timeout is scaled by the worker count. The budget is derived from the *serial*
    baseline (`_derive_timeout` = 10x its wall-clock), but W workers contend for CPU/RAM, so each
    suite runs up to ~Wx slower in wall-clock than it did alone. Without scaling, a genuinely
    *passing* suite could exceed its serial budget under contention and be misrecorded as a TIMEOUT
    (scored as killed) — silently hiding a survivor. Multiplying the budget by W restores the same
    10x headroom in per-worker-second terms, so a timeout still means a real hang, not a lost CPU
    lottery (fail toward slower, never toward fewer — the prime directive).
    """
    total = len(mutants)
    rel = os.path.relpath(Path(path).resolve(), Path(project_dir).resolve())
    outcomes: dict[int, MutantOutcome] = {}

    # Serial pre-pass: resolve ignored/invalid without Godot, and apply (gdtoolkit) single-threaded.
    # Runnable mutants carry their already-applied source into the parallel run.
    runnable: list[tuple[int, Mutant, str]] = []
    for index, mutant in enumerate(mutants):
        if mutant.ignore_reason is not None:
            outcome = MutantOutcome(mutant, Verdict.IGNORED)
        else:
            mutated, valid = adapter.apply_mutant(mutant, source)
            if valid:
                runnable.append((index, mutant, mutated))
                continue
            outcome = MutantOutcome(mutant, Verdict.INVALID)
        outcomes[index] = outcome
        if progress is not None:
            progress(_progress_line(index + 1, total, outcome))
    if not runnable:  # every mutant was ignored/invalid — no Godot run needed
        return [outcomes[index] for index in range(total)]

    worker_count = min(jobs, len(runnable))
    contention_timeout = per_mutant_timeout * worker_count
    work: queue.Queue[tuple[int, Mutant, str]] = queue.Queue()
    for item in runnable:
        work.put(item)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(worker_dir: str) -> None:
        target = str(Path(worker_dir) / rel)
        while True:
            try:
                index, mutant, mutated = work.get_nowait()
            except queue.Empty:
                return
            try:
                verdict = _run_one(worker_dir, target, source, mutated, runner, contention_timeout)
            except BaseException as exc:  # noqa: BLE001 — capture + re-raise in the main thread
                with lock:
                    errors.append(exc)
                return
            outcome = MutantOutcome(mutant, verdict)
            with lock:
                outcomes[index] = outcome
                if progress is not None:
                    progress(_progress_line(index + 1, total, outcome))

    with tempfile.TemporaryDirectory(prefix="gdmutant-jobs-") as tmp:
        threads: list[threading.Thread] = []
        for w in range(worker_count):
            worker_dir = str(Path(tmp) / f"w{w}")
            shutil.copytree(project_dir, worker_dir)
            thread = threading.Thread(target=worker, args=(worker_dir,))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    return [outcomes[index] for index in range(total)]


def run_paths(
    project_dir: str,
    sources: dict[str, str],
    runner: Runner,
    adapter: Adapter,
    catalog: tuple[Operator, ...] = CATALOG,
    *,
    timeout: float | None = None,
    progress: Callable[[str], None] | None = None,
    jobs: int = 1,
) -> dict[str, MutationRun]:
    """Mutate several files against one project — the baseline runs **once**, then each file's
    mutants run in turn (LOD-79). `sources` maps each file path to its contents (each held on entry,
    restored after its own mutants). `adapter` is the injected language adapter (NF-3, like `run`).
    `jobs` parallelizes each file's mutants exactly as `run` does. Returns ``{path: MutationRun}``
    in `sources` order. Raises `BaselineFailed` (like `run`) if the baseline can't run or is red.
    """
    per_mutant_timeout, baseline_secs = _run_baseline(project_dir, runner, timeout, progress)
    runs: dict[str, MutationRun] = {}
    for path, source in sources.items():
        if progress is not None:
            progress(f"mutating {path} ...")
        runs[path] = _mutate_file(
            project_dir,
            path,
            source,
            runner,
            adapter,
            per_mutant_timeout,
            baseline_secs,
            catalog,
            progress,
            jobs,
        )
    return runs


def _run_one(
    project_dir: str, path: str, source: str, mutated: str, runner: Runner, timeout: float
) -> Verdict:
    target = Path(path)
    try:
        target.write_text(mutated, encoding="utf-8")
        try:
            result = runner.run(project_dir, timeout=timeout)
        except SuiteTimeout:
            # The mutation hung the suite — a detection, not a crash. Count it as killed
            # (Stryker's Timeout status), distinct from ERROR below.
            return Verdict.TIMEOUT
        except Exception:
            # The runner failed to execute this mutant (e.g. a Godot crash). Tally it as ERROR and
            # carry on — one bad run must not discard the whole pass (FG-4.1).
            return Verdict.ERROR
        return Verdict.KILLED if result.failed else Verdict.SURVIVED
    finally:
        target.write_text(source, encoding="utf-8")  # never leave the project mutated
