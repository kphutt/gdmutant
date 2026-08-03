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

import math
import os
import queue
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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


class SourceOutsideProject(Exception):
    """A file to mutate does not lie inside the project directory, so `jobs > 1` cannot isolate it.

    Parallel evaluation works by giving each worker its own copy of the project and mutating the
    file *inside that copy*. A file outside the project is in no copy, so there is nothing to
    isolate and nothing sound to run — see `_project_relative`.
    """


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


class ProgressStyle(Enum):
    """How chatty the periodic heartbeat is. The *policy* (is this a terminal? is this CI?) belongs
    to the caller — the engine only takes the answer, so it reads no environment of its own."""

    #: A terminal with someone watching: a heartbeat at most every `_HEARTBEAT_SECS`.
    RICH = "rich"
    #: A log file or a CI job: rarer, so a two-hour run does not bury the build log. Both rules must
    #: be satisfied, which makes the *rarer* of the two win.
    PLAIN = "plain"
    #: No heartbeat at all. A caller that still passes a `progress` callback keeps the plan line and
    #: the closing wall-clock: those are facts about the run, not progress chatter. gdmutant's own
    #: CLI is not such a caller — under ``--progress none`` it passes no callback at all
    #: (`cli._progress_emitter`), so nothing this class governs reaches a terminal. The distinction
    #: matters here because this style alone cannot deliver silence: the emitter is what decides.
    NONE = "none"


_HEARTBEAT_SECS = 30.0  # RICH: never more often than this, so the heartbeat can't become the noise
_HEARTBEAT_SECS_PLAIN = 60.0  # PLAIN: Infection's CI cadence
_HEARTBEAT_FRACTION_PLAIN = 0.10  # …and at least a tenth of the mutants, whichever is rarer
#: gdmutant forecasts **no** finish time — not up front, and not from measured throughput either. A
#: rate extrapolation was built and measured against a real Godot project before being dropped: on
#: an even workload it tracked the true finish to within 5%, but on the shape that actually matters
#: — a cluster of hanging mutants arriving after the rate has settled on the fast ones — it read
#: 3.2s at 25% done for a run that took 58.0s. That is a 95% under-read, and 18x worse than the up-
#: front estimate it was meant to replace, because a mutant that hangs costs its whole timeout and
#: by construction nothing before it hinted that it would. Stryker, the one peer that forecasts at
#: all, has the same failure the other way round: stryker-js#4018 shows `remaining: ~12h 51m` on a
#: run that finished in 10m 26s, off by 74x with 913 samples behind it. So the heartbeat reports
#: only what has actually happened. Elapsed time, counts and the closing wall-clock are facts; a
#: finish time is a guess this tool has no way to make honestly.


def _contention_budget(per_mutant_timeout: float, workers: int) -> float:
    """The per-mutant time budget actually enforced when `workers` suites run at once.

    The single source of that arithmetic: `_run_mutants_parallel` enforces it and `_progress_plan`
    announces it, and when the two computed it separately the announcement drifted — it named the
    unscaled figure while the run allowed up to N times that. The number is the answer to "how long
    is silence normal?", so an announcement that understates it is exactly the thing that makes a
    healthy run look hung. `_run_mutants_parallel` explains why the scaling itself is right.
    """
    return per_mutant_timeout * max(workers, 1)


def _progress_plan(
    runnable: int, total: int, baseline_secs: float, per_mutant_timeout: float, jobs: int
) -> str:
    """The pre-run line: **what the run is**, with no prediction of how long it will take.

    gdmutant used to print ``estimated ≈ 24s`` here, from mutant count × baseline time. No other
    mutation tester forecasts an absolute duration before the work starts, and the one that
    forecasts at all (Stryker) derives it from *measured* throughput and still misses badly. The
    figure was wrong in both directions at once: too low by 1.7–3.4× on a real project (it counted
    neither gdmutant's own per-mutant work nor the timeouts, which were four minutes of one 6m24s
    run), and — because it never took `jobs` — roughly N× too high under ``--jobs N``. An estimate
    whose error direction is not even stable is worse than no estimate.

    What it must keep doing is **pace the wait**. "Looks hung" is the #1 documented reason people
    abandon mutation testing, and the first Godot boot is real silence. So the budget clause stays,
    and does the job better than a total ever did: *how long silence is normal* is exactly what
    someone staring at a still terminal needs, and unlike a forecast it is a fact.

    Which is why the cap it names is the **contention-scaled** one under ``--jobs``: that is what
    the run enforces (`_contention_budget`), and naming the unscaled figure instead understated the
    real worst case by up to N times — turning the one clause that paces the wait into the reason a
    healthy run looks hung. `min(jobs, runnable)` is the worker count the parallel path will pick;
    it can only over-count (a mutant that turns out invalid never takes a worker), which errs toward
    naming a longer silence than will happen — the safe direction for this particular fact.
    """
    unit = "mutant" if runnable == 1 else "mutants"
    ignored = total - runnable
    ignored_note = f" ({ignored} ignored)" if ignored else ""
    jobs_note = f" Running {jobs} at a time." if jobs > 1 else ""
    budget = _contention_budget(per_mutant_timeout, min(jobs, runnable))
    return (
        f"{runnable} {unit} to run{ignored_note}. Baseline suite {baseline_secs:.1f}s; "
        f"each mutant is capped at {budget:.1f}s.{jobs_note}"
    )


@dataclass
class _Progress:
    """The run's own stopwatch: what has finished, how long it has taken, and the two lines nothing
    else can produce — the periodic heartbeat and the closing wall-clock.

    Two scopes, deliberately. The **heartbeat** counts one file's mutants, because that is the
    denominator that exists while the run is going (a directory run generates each file's mutants
    only when it reaches it). The **closing line** covers the whole run, because that is the number
    a user takes away. `begin_file` resets the first without disturbing the second.

    Nothing here predicts a finish time, deliberately and on evidence — the note above
    `_progress_plan` has the measurement. Everything it prints is something that already happened,
    which also means ``--jobs`` needs no special case: parallel work simply makes the counts climb
    faster, and no arithmetic depends on how many workers there are.

    Under ``--jobs`` the workers call `record` while holding the loop's lock, so the counters and
    the last-beat marks are only ever touched by one thread at a time.
    """

    emit: Callable[[str], None] | None
    style: ProgressStyle
    baseline_secs: float
    #: Whole-run: when the first mutant work started, and what it cost.
    # A lambda, not `time.monotonic` itself: the bare function would be captured at class
    # definition, leaving `started` on a different clock from every other reading here the
    # moment a test (or a caller) substitutes one.
    started: float = field(default_factory=lambda: time.monotonic())
    ran: int = 0
    timeouts: int = 0
    timeout_secs: float = 0.0
    #: This file: the heartbeat's numerator, denominator and last-emitted marks.
    total: int = 0
    done: int = 0
    survived: int = 0
    file_timeouts: int = 0
    last_beat: float = 0.0
    last_beat_done: int = 0

    def begin_file(self, total: int) -> None:
        """Start counting a new file's `total` runnable mutants."""
        self.total = total
        self.done = 0
        self.survived = 0
        self.file_timeouts = 0
        self.last_beat = time.monotonic()
        self.last_beat_done = 0

    def record(self, verdict: Verdict, elapsed: float) -> None:
        """Tally one finished mutant and the wall-clock it cost, then heartbeat if it is due."""
        self.done += 1
        self.ran += 1
        if verdict is Verdict.SURVIVED:
            self.survived += 1
        elif verdict is Verdict.TIMEOUT:
            self.file_timeouts += 1
            self.timeouts += 1
            # Measured, not `timeouts × budget`: under --jobs the budget is scaled and the waits
            # overlap, so only the real elapsed time is true on both paths.
            self.timeout_secs += elapsed
        self.beat()

    def beat(self, *, force: bool = False) -> None:
        """Emit the heartbeat if it is due (or `force`d, at the end of a file).

        The forced beat is not decoration. Under `ProgressStyle.PLAIN` a whole file can finish
        inside one interval and emit nothing at all, leaving a log that never shows the work
        reaching its end — stryker-js#5929 is that exact bug, still open. Forcing one guarantees a
        line at n/n for every file.
        """
        if self.emit is None or self.style is ProgressStyle.NONE:
            return
        now = time.monotonic()
        if not force:
            if self.style is ProgressStyle.PLAIN:
                due_secs, due_count = _HEARTBEAT_SECS_PLAIN, _plain_beat_every(self.total)
            else:
                due_secs, due_count = _HEARTBEAT_SECS, 1
            # Both rules must pass, so the rarer of the two is what actually governs.
            if now - self.last_beat < due_secs or self.done - self.last_beat_done < due_count:
                return
        self.last_beat = now
        self.last_beat_done = self.done
        self.emit(self._heartbeat_line(now - self.started))

    def _heartbeat_line(self, elapsed: float) -> str:
        """Progress as measurement only — how much is done, how long it has taken, and what has
        been found so far. No finish time: see `_HEARTBEAT_SECS`' note for the run that settled
        that."""
        return (
            f"… {self.done}/{self.total} done in {_format_duration(elapsed)} — "
            f"{self.survived} survived, {self.file_timeouts} timed out."
        )

    def finish(self) -> None:
        """Emit the closing wall-clock line — the number every other test runner prints and this one
        did not, with the timeout cost broken out because that is the cost nobody can see. On the
        measured run, eight timeouts were four minutes of six and a half, invisible at both ends."""
        if self.emit is None:
            return
        elapsed = time.monotonic() - self.started
        unit = "mutant" if self.ran == 1 else "mutants"
        cost = (
            f"{self.timeouts} timed out ({_format_duration(self.timeout_secs)} of that)"
            if self.timeouts
            else "none timed out"
        )
        self.emit(
            f"Done in {_format_duration(elapsed)} — {self.ran} {unit}, {cost}. "
            f"Baseline suite {self.baseline_secs:.1f}s."
        )


def _plain_beat_every(total: int) -> int:
    """Mutants per heartbeat under `ProgressStyle.PLAIN`: a tenth of the file, at least one."""
    return max(1, math.ceil(total * _HEARTBEAT_FRACTION_PLAIN))


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
    progress_style: ProgressStyle = ProgressStyle.RICH,
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

    If `progress` is given, it is called with the baseline notice, the pre-run plan
    (`_progress_plan`), then a rate-limited heartbeat (`_Progress.beat`) as mutants finish — never
    one line per mutant: that repeated the per-mutant timeout budget over and over for no reason,
    once the plan line had already said it. The CLI wires `progress` to stderr so a long or hanging
    run shows steady output instead of looking frozen; `None` runs silently. `jobs > 1` needs no
    special case here — the heartbeat only ever reports aggregate counts, which climb the same way
    whether one worker or several produced them.
    """
    per_mutant_timeout, baseline_secs = _run_baseline(project_dir, runner, timeout, progress)
    clock = _Progress(emit=progress, style=progress_style, baseline_secs=baseline_secs)
    result = _mutate_file(
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
        clock,
    )
    clock.finish()
    return result


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
    # inflates the baseline wall-clock that derives per-mutant timeouts and the ETA. A
    # runner with nothing to prepare simply isn't Preparable — the engine stays language-neutral.
    if isinstance(runner, Preparable):
        if progress is not None:
            # Name the wait. This step is a cold-checkout asset import for the Godot runners, which
            # on a real game runs for minutes with nothing on screen — the shape a first-time user
            # reads as a hung tool. The engine stays language-neutral by describing the *cost*, not
            # what the setup is.
            progress("preparing the project (one-time; on a fresh checkout this can take minutes)")
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
    # A baseline that ran ZERO tests is not a green baseline — it is no baseline at all, and it is
    # the quietest way this tool can lie. `SuiteResult(0, 0, 0).failed` is False, so a suite nobody
    # ran reads exactly like a suite that passed, and then every single mutant comes back SURVIVED:
    # a whole report of false survivors with no error anywhere in the run. That is gdmutant's worst
    # failure mode, produced by a typo in a path.
    #
    # **This check lives here, in the engine, on purpose** — "the baseline ran no tests" is a
    # property of a baseline, not of a language or a framework, and it is reachable under ANY runner
    # whose test path or project is misconfigured. Before this, the only such check anywhere lived
    # in one adapter (`GutRunner`), which left the other two runners unguarded; the same guard
    # copied per adapter would drift apart the moment a fourth runner is added — the adapters do
    # still keep theirs, for the reason below. Language-neutrality (NF-3) is kept because this
    # check reads only `SuiteResult.tests` and names no framework.
    #
    # The adapters still keep their own zero-test guards, and that is not duplication: this one
    # states the *condition*, while an adapter can state the *cause and the cure* — GUT's `-gdir`
    # does not recurse, GdUnit4 prints "No test cases found" — which the engine must not know. An
    # adapter that raises first simply wins with the better message, and this stays the backstop for
    # every runner that has nothing framework-specific to say (the exit-code `CommandRunner`, and
    # any runner added later).
    if baseline.tests == 0:
        raise BaselineFailed(
            f"the unmutated (baseline) test suite for {project_dir!r} reported 0 tests. Nothing "
            "ran, so nothing can be detected: every mutant would come back SURVIVED and the whole "
            "report would be false. This is a discovery or configuration problem rather than a red "
            "suite — check that the runner is pointed at your tests (--tests, or --command for a "
            "custom harness) and that the suite runs on its own."
        )
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
    jobs: int,
    clock: _Progress,
) -> MutationRun:
    """Generate and run every mutant for a single file (the baseline is assumed already green). The
    file at `path` must hold `source`; it is restored before returning. `jobs > 1` evaluates mutants
    concurrently on per-worker project copies, then reassembles them in generation order."""
    mutants = adapter.generate_mutants(path, source, catalog)
    total = len(mutants)
    runnable = sum(1 for m in mutants if m.ignore_reason is None)
    clock.begin_file(runnable)
    if progress is not None:
        progress(_progress_plan(runnable, total, baseline_secs, per_mutant_timeout, jobs))
    if jobs > 1 and total > 0:
        outcomes = _run_mutants_parallel(
            project_dir,
            path,
            source,
            runner,
            adapter,
            mutants,
            per_mutant_timeout,
            jobs,
            clock,
        )
    else:
        outcomes = _run_mutants_serial(
            project_dir, path, source, runner, adapter, mutants, per_mutant_timeout, clock
        )
    clock.beat(force=True)  # every file ends on a line that shows it reached n/n
    return MutationRun(tuple(outcomes))


def _run_mutants_serial(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    mutants: Sequence[Mutant],
    per_mutant_timeout: float,
    clock: _Progress,
) -> list[MutantOutcome]:
    """Evaluate every mutant one at a time against the real project file (the trusted default).

    No per-mutant line here on purpose — only `clock.record`'s own rate-limited heartbeat
    (`_Progress.beat`, via `clock.emit`) reports progress. A line for every mutant repeated the
    per-mutant timeout budget over and over (the old "running (<=Ns) ..."), which is noise once
    the pre-run plan line has already said it once; the heartbeat already answers "is this still
    going" without repeating a number nobody needs restated per mutant.
    """
    outcomes: list[MutantOutcome] = []
    for mutant in mutants:
        ran: float | None = None
        if mutant.ignore_reason is not None:
            # A `# gdmutant: ignore` annotation suppresses this mutant — generated for the report
            # but never run (no validity check, no suite run): tallied IGNORED, excluded from score.
            verdict = Verdict.IGNORED
        else:
            mutated, valid = adapter.apply_mutant(mutant, source)
            if valid:
                started = time.monotonic()
                verdict = _run_one(project_dir, path, source, mutated, runner, per_mutant_timeout)
                ran = time.monotonic() - started
            else:
                verdict = Verdict.INVALID
        outcome = MutantOutcome(mutant, verdict)
        outcomes.append(outcome)
        if ran is not None:
            # Only a mutant that actually ran is a sample: ignored and invalid ones never reached
            # the suite, so counting them would make the measured rate a fiction.
            clock.record(verdict, ran)
    return outcomes


def _project_relative(path: str, project_dir: str) -> str:
    """Where `path` sits inside `project_dir`, as a relative path. Raises if it sits outside.

    Each parallel worker mutates ``<its copy of the project>/<this>``. That address is only inside
    the copy while the file is genuinely under the project, and nothing used to check: the relative
    path was taken as-is, so a source outside the project produced one starting with ``..`` and the
    worker wrote *through* its own copy and out the other side.

    What that cost was not a stray file so much as a wrong answer. The mutation never landed in the
    copy the tests were about to run against, so every mutant came back SURVIVED — gdmutant's worst
    failure mode, a survivor report that is quietly false. Every worker also computed the *same*
    outside address, so they raced each other on one file. And the further out the file sat, the
    further out the write went: enough ``..`` segments and it left the temporary directory
    altogether, landing wherever that resolved to.

    Refusing is the honest answer rather than a silent repair. There is no copy of this file for a
    worker to mutate, so there is no isolated run to be had — only a serial one, against the real
    file, which is what `jobs=1` already does.
    """
    source = Path(path).resolve()
    project = Path(project_dir).resolve()
    try:
        return str(source.relative_to(project))
    except ValueError:
        # relative_to covers both "outside the project" and Windows' separate-drive case, which
        # os.path.relpath used to raise on unhandled, taking the whole run down with a traceback.
        raise SourceOutsideProject(
            f"{path} is not inside the project directory {project_dir}, so --jobs cannot give it "
            "an isolated copy to mutate. Point --project at a directory containing it, or drop "
            "--jobs to run serially."
        ) from None


def _run_mutants_parallel(
    project_dir: str,
    path: str,
    source: str,
    runner: Runner,
    adapter: Adapter,
    mutants: Sequence[Mutant],
    per_mutant_timeout: float,
    jobs: int,
    clock: _Progress,
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
    rel = _project_relative(path, project_dir)
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
    if not runnable:  # every mutant was ignored/invalid — no Godot run needed
        return [outcomes[index] for index in range(total)]

    worker_count = min(jobs, len(runnable))
    contention_timeout = _contention_budget(per_mutant_timeout, worker_count)
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
            started = time.monotonic()
            try:
                verdict = _run_one(worker_dir, target, source, mutated, runner, contention_timeout)
            except BaseException as exc:  # noqa: BLE001 — capture + re-raise in the main thread
                with lock:
                    errors.append(exc)
                return
            ran = time.monotonic() - started
            outcome = MutantOutcome(mutant, verdict)
            with lock:
                outcomes[index] = outcome
                # clock.record emits the rate-limited heartbeat itself; no per-mutant line here
                # (see _run_mutants_serial's docstring). Inside the same lock so the counters and
                # the last-beat marks are only ever touched by one worker at a time.
                clock.record(verdict, ran)

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
    progress_style: ProgressStyle = ProgressStyle.RICH,
) -> dict[str, MutationRun]:
    """Mutate several files against one project — the baseline runs **once**, then each file's
    mutants run in turn. `sources` maps each file path to its contents (each held on entry,
    restored after its own mutants). `adapter` is the injected language adapter (NF-3, like `run`).
    `jobs` parallelizes each file's mutants exactly as `run` does. Returns ``{path: MutationRun}``
    in `sources` order. Raises `BaselineFailed` (like `run`) if the baseline can't run or is red.
    """
    per_mutant_timeout, baseline_secs = _run_baseline(project_dir, runner, timeout, progress)
    clock = _Progress(emit=progress, style=progress_style, baseline_secs=baseline_secs)
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
            clock,
        )
    # One closing line for the whole run, not one per file: the wall-clock a user takes away is
    # "how long did that take", and every file's mutants are part of the same wait.
    clock.finish()
    return runs


def _detect_eol(target: Path) -> str:
    """The line ending `target` actually uses on disk, so it can be restored byte-exactly.

    Callers hand us source that was read with `read_text`, which normalises CRLF to LF. Writing
    it back with `write_text` then translates LF to `os.linesep` — so on Windows a
    mutate-then-restore cycle silently rewrites the file with CRLF. Against a project whose
    `.gitattributes` declares `eol=lf` that leaves every mutated file permanently "modified"
    with an empty diff, which is exactly the noise `_run_one`'s finally-block exists to prevent.
    Sampling the real ending first is what makes "never leave the project mutated" true at the
    byte level rather than only at the text level.
    """
    try:
        with open(target, "rb") as handle:
            return "\r\n" if b"\r\n" in handle.read() else "\n"
    except OSError:
        return "\n"


#: How many times to try the final rename before giving up. Windows refuses it while another
#: process holds the destination open (see `_write_source`), and those holders — a virus scanner,
#: a search indexer, an editor reloading the file — let go in milliseconds, so a handful of short
#: retries turn almost every one of them into a non-event.
_REPLACE_ATTEMPTS = 6
#: Seconds to wait after the first failed rename; each further wait grows by this much again, so
#: the whole budget is about a second and a half.
_REPLACE_BACKOFF = 0.1


class SourceWriteFailed(Exception):
    """gdmutant could not write a source file — and did not damage it trying.

    Raised only where the destination is still exactly as it was a moment ago, so this write left
    nothing half-written behind: it is raised *instead of* attempting a write that could.

    That is a promise about the **write**, not about the **run**. `_write_source` runs twice per
    mutant, once to put the mutant in and once to put the original back, and a failure on the
    second one leaves the mutant on disk — whole, readable, and not what the user wrote. So
    "undamaged" can still mean "holding a mutant". Every message raised with this therefore says
    which of the two is sitting there and points at git, and a caller must not report it as
    reassurance that the user's own source survived.
    """


def _replace_with_retry(temp: Path, dest: Path) -> None:
    """Move `temp` onto `dest`, retrying briefly while the destination is locked.

    ``os.replace`` is the atomic step, but on Windows it raises ``PermissionError`` whenever
    another process has the destination open — *even just for reading*, which an editor, an
    antivirus scanner, or the test engine that gdmutant itself just launched all do routinely. On
    POSIX the same call simply succeeds. The retries cover the transient Windows holders; a lock
    that outlasts them lets the error escape, and `_write_source` turns that into a clean refusal
    rather than a write it cannot make safely.
    """
    for attempt in range(_REPLACE_ATTEMPTS - 1):
        try:
            os.replace(temp, dest)
            return
        except PermissionError:
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    os.replace(temp, dest)


def _write_source(target: Path, text: str, eol: str) -> None:
    """Write LF-normalised `text` to `target` using `eol`, or leave `target` exactly as it was.

    `target` is the user's own source file, and a mutation run rewrites it twice per mutant. A
    plain in-place write truncates the file to nothing before putting anything back, so a crash, a
    power cut, or a hard kill landing in that window destroys the file outright — not "left
    holding a mutant", which a reader can see and undo, but left empty or cut off mid-token. The
    window is small and it is hit constantly: the run spends most of its time between those two
    writes.

    So the bytes go to a temporary file beside the target, get flushed all the way to the disk,
    and are then moved onto the target with ``os.replace``. A rename within one directory is a
    single filesystem operation, so at every instant the path holds either the whole old file or
    the whole new one.

    Four details make that hold in practice:

    * The temporary file is created in the target's **own directory**, so the move never crosses a
      filesystem — across one it would degrade to a copy, which is exactly the non-atomic write
      being avoided.
    * A temporary file is created private to its owner, so its permissions are copied from the
      target first; otherwise the rename would quietly tighten the source file's permissions.
    * ``os.replace`` would swap a **symbolic link** for a regular file and leave the file it names
      untouched, so a link is resolved to its destination up front and the real file is the one
      rewritten — matching what a plain in-place write does.
    * A rename needs write permission on the *directory*, not on the file, so it would happily
      replace a file the user marked **read-only** — which a plain write refuses to do. That
      permission is a deliberate instruction, so it is checked first and honoured.

    **There is no degraded path.** Anything that stops the staged write — no room for a temporary
    file, a failed flush, a lock that outlasts the retries — raises `SourceWriteFailed` with the
    destination untouched. An earlier version fell back to writing in place, on the reasoning that
    a write as unsafe as the old one still beat no write at all. That was wrong, and a review
    proved it: one persistent fault, a full disk being the obvious one, hits the staged write and
    the fallback alike, and the fallback had already truncated the file by the time its own write
    failed. A tool promising never to leave your source half-written cannot keep a path that does
    exactly that. Failing loudly with the file intact is the honest answer.
    """
    # Resolve a symlink to the file it points at: the rename below replaces whatever sits at the
    # path it is given, and replacing the link itself would leave the real source unwritten.
    dest = Path(os.path.realpath(target))
    body = text if eol == "\n" else text.replace("\n", eol)
    exists = dest.exists()
    if exists and not os.access(dest, os.W_OK):
        # The same disambiguation the other two refusals carry, and needed here for the same
        # reason. This helper is also what restores the original after a mutant, so a file that
        # turns read-only between those two writes fails on the restore, and the mutant is what is
        # left sitting there. Nothing in "is read-only" tells the reader that.
        raise SourceWriteFailed(
            f"{dest} is read-only. gdmutant rewrites the files it mutates, so it will not "
            "silently override that. Make the file writable, or leave it out of the run. "
            "Nothing was written, so the file still holds whatever was in it a moment ago. "
            "If gdmutant had already put a mutant there, the mutant is what is on disk now, "
            "not your original. Restore the file from git before trusting it."
        )
    try:
        handle_fd, temp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
        )
    except OSError as error:
        # Same care as the rename failure below, and for the same reason: "left untouched" is true
        # of this write, but a reader takes it as a promise about their *original*, and this same
        # helper is what puts the original back after a mutant. Say which of the two is there.
        raise SourceWriteFailed(
            f"could not create a temporary file next to {dest} ({error}), so {dest.name} could "
            "not be rewritten safely. Nothing was written, so the file still holds whatever was "
            "in it a moment ago. If gdmutant had already put a mutant there, the mutant is what "
            "is on disk now, not your original. Restore the file from git before trusting it."
        ) from error
    temp = Path(temp_name)
    try:
        with open(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            # Push the bytes past the OS cache before the rename: without this the rename can land
            # first and a power cut would expose a file that is atomically... empty.
            os.fsync(handle.fileno())
        if exists:
            shutil.copymode(dest, temp)
        _replace_with_retry(temp, dest)
    except OSError as error:
        # Deliberately NOT "left exactly as it was": true of this write, but a reader takes it as a
        # promise about their *original*, and this same helper is what puts the original back after
        # a mutant. A restore that fails here leaves the mutant on disk, so say that plainly.
        raise SourceWriteFailed(
            f"could not rewrite {dest} ({error}). This write changed nothing, so the file still "
            "holds whatever was in it a moment ago. If gdmutant had already put a mutant there, "
            "the mutant is what is on disk now, not your original. Restore the file from git "
            "before trusting it."
        ) from error
    finally:
        # A successful rename consumed the temporary file; this clears it after a failure.
        temp.unlink(missing_ok=True)


def _run_one(
    project_dir: str, path: str, source: str, mutated: str, runner: Runner, timeout: float
) -> Verdict:
    target = Path(path)
    # Sample before the first write, while the file still holds the unmutated source.
    eol = _detect_eol(target)
    try:
        _write_source(target, mutated, eol)
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
        if result.tests == 0 and not result.failed:
            # Zero tests, no failures — the same silent lie the baseline guard refuses, one mutant
            # at a time. The baseline already proved this project collects tests (`_run_baseline`
            # refuses a zero-test baseline), so a mutant run that collects none did not "pass": test
            # collection collapsed, most likely because the mutant broke a file the suites load. A
            # SURVIVED verdict here is a false survivor. ERROR is the honest one — the same verdict
            # a raising runner gets, and it is excluded from the score rather than inflating it.
            #
            # Language-neutral, and the counterpart of the baseline check above: it reads only
            # `SuiteResult.tests`, so it covers any runner whose framework zeroes a run *without*
            # raising. An adapter that can recognise the shape still raises first with a better
            # message (`GutRunner` does); this catches the ones that cannot.
            #
            # Deliberately unreachable for every runner that ships TODAY: both JUnit adapters raise
            # on a zero-test report before returning one, and `CommandRunner` reports `tests=1` for
            # any exit-0 run because an exit code carries no count. It is the backstop a fourth
            # runner inherits without having to know it exists — which is the whole reason the
            # check moved here instead of being copied into a third adapter.
            return Verdict.ERROR
        return Verdict.KILLED if result.failed else Verdict.SURVIVED
    finally:
        _write_source(target, source, eol)  # never leave the project mutated
