"""The test-runner interface + JUnit-XML result parsing (language-neutral).

The engine drives a `Runner` (Slice 5): apply a mutant, run the target's suite, read a
`SuiteResult`. A mutant is **killed** when the suite fails and **survives** when it passes. JUnit
XML is a cross-language standard, so its parser lives here; the concrete Godot/GdUnit4 runner is a
GDScript-adapter concern that lands with the end-to-end slice (it needs real Godot to validate).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from xml.etree import ElementTree


def with_filename(error: FileNotFoundError, attempted: str) -> FileNotFoundError:
    """`error`, guaranteed to carry a `.filename` the CLI's missing-executable hint can show the
    user.

    On POSIX, ``subprocess.run`` failing to exec a missing binary sets `.filename` to the attempted
    path. On Windows, the underlying ``CreateProcess`` failure does not — `.filename` comes back
    `None` — so the CLI's hint silently falls back to a generic "the test runner" placeholder and
    never names the actual (wrong) executable path, on the one platform this project treats as a
    deployment target (see AGENTS.md). Reusing `error`'s own errno/strerror when present keeps the
    real OS message; only the filename is patched in when it's missing.

    Lives here (language-neutral, `engine.runner`) rather than in the GDScript adapter because
    every `Runner` that shells out via `subprocess.run` hits the same Windows quirk — the adapter's
    two Godot call sites and the framework-neutral `CommandRunner` below all use it."""
    if error.filename is not None:
        return error
    return FileNotFoundError(error.errno, error.strerror, attempted)


class SuiteTimeout(Exception):
    """The test suite exceeded its time budget.

    Distinct from a generic runner error: a mutation that makes the suite *hang* (an infinite loop)
    is a **detection** — the change altered behavior observably — so the loop counts a timeout as
    killed (Stryker's ``Timeout`` status), not as an ``error``. Runners raise this instead of
    letting ``subprocess.TimeoutExpired`` leak, so the engine stays subprocess-agnostic.
    """


@dataclass(frozen=True)
class SuiteResult:
    """The aggregate outcome of running a test suite once."""

    tests: int
    failures: int
    errors: int
    skipped: int = 0
    #: Optional runner-supplied diagnostic (e.g. a failing command's captured output). Surfaced in
    #: the *baseline*-failure message so a first run that can't even go green is debuggable; ignored
    #: for per-mutant results, so it adds no noise during the run.
    detail: str = ""

    @property
    def failed(self) -> bool:
        """True if any test failed or errored — the signal that a mutant was killed."""
        return self.failures > 0 or self.errors > 0

    @property
    def passed(self) -> bool:
        return not self.failed


@runtime_checkable
class Runner(Protocol):
    """Runs a target project's test suite once and reports the aggregate result.

    `timeout` overrides the runner's own budget for this call (seconds); ``None`` uses the runner's
    configured default. The engine derives a per-mutant budget from the baseline run and passes it
    here, so a hanging mutant is cut off in seconds rather than blocking for the full default.

    **Crash-safety contract (the property every runner must uphold).** A mutation that makes a test
    file fail to *load or compile* must surface as a **kill or an error** — **never** a silent
    zero-test *pass*. A runner that returned "0 tests, 0 failures" for such a crash would mark the
    responsible mutant SURVIVED, gdmutant's single worst failure mode (a wrong survivor report).
    Each concrete adapter upholds this in the way its framework fails:
      * ``CommandRunner`` — a non-zero exit is a failure (killed); a command that can't be executed
        at all raises (the engine tallies ``error``).
      * ``GdUnit4Runner`` — a crash writes *no* report, caught by the "the report must reappear"
        freshness guard (it raises → ``error``).
      * ``GutRunner`` — GUT *skips* a suite that fails to load and runs the rest green (exit 0), so
        it raises on both ``tests == 0`` *and* a drop below the healthy baseline's test count (a
        skipped suite) → ``error``, never a false survivor.
    """

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult: ...


@runtime_checkable
class Preparable(Protocol):
    """A runner that needs a one-time, potentially slow setup step before the baseline run.

    The engine calls `prepare` once, *before* it starts timing the baseline — so the setup cost
    never leaks into the baseline wall-clock that derives per-mutant timeouts and the progress ETA.
    Optional: a runner that needs no setup simply doesn't implement it (the engine skips it via an
    ``isinstance`` check), so this stays language-neutral (NF-3) — the engine never names what the
    setup *is*. `prepare` must be idempotent: it may also be called defensively from ``run``.
    """

    def prepare(self, project_dir: str) -> None: ...


@runtime_checkable
class RunWarning(Protocol):
    """A runner that may surface a single **run-level warning** once the whole mutation run ends.

    Optional (checked via ``isinstance``, exactly like `Preparable`), so the engine/CLI stays
    language-neutral (NF-3): a runner with nothing to say simply doesn't implement it. `run_warning`
    is called once, *after* the run completes, and returns a stderr warning string, or ``None`` when
    nothing is amiss.

    Unlike a per-mutant error, this **never** changes the mutation score or the exit code — it flags
    a condition the operator should investigate (e.g. `GutRunner`'s non-determinism canary: test
    collection that varies run-to-run, degrading the crash-safety drop-guard), on the same stderr
    surface as the "all mutants survived" warning.
    """

    def run_warning(self) -> str | None: ...


@dataclass(frozen=True)
class CommandRunner:
    """Runs an arbitrary test command and maps its **exit code** to a `SuiteResult`: exit 0 means
    the suite passed, any non-zero exit means it failed.

    For projects whose test harness signals pass/fail via the exit code and produces no JUnit XML —
    e.g. a hand-rolled ``godot --headless --script res://tests/run_tests.gd`` runner. This is
    language- and framework-neutral (it only shells out and reads the exit code), so it lives in the
    engine, not an adapter. The convention is documented in docs/decisions/0005.

    The exit code is a coarser signal than JUnit XML: it can't separate a *test failure* from the
    harness itself *erroring* (both are non-zero), so a mutant that makes the run crash counts as
    killed. The NF-5 re-parse guard still filters mutants that don't parse before they ever run, and
    a command that can't be executed at all raises (the engine tallies that as ERROR).
    """

    command: Sequence[str]
    timeout: float = 600.0

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        budget = self.timeout if timeout is None else timeout
        try:
            completed = subprocess.run(
                list(self.command),
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=budget,
            )
        except subprocess.TimeoutExpired as expired:
            # A mutation-induced hang is a detection, not a crash — surface it distinctly so the
            # engine tallies Timeout (killed), not error. subprocess.run has already killed the
            # child process by the time it raises.
            raise SuiteTimeout(f"test command exceeded {budget:g}s") from expired
        except FileNotFoundError as error:
            # On Windows this arrives with .filename == None (see `with_filename`), so the CLI's
            # missing-executable hint would otherwise fall back to a generic placeholder instead of
            # naming the actual bad command — the same Windows quirk the GdUnit4/GUT call sites
            # patch around.
            raise with_filename(error, self.command[0]) from error
        if completed.returncode == 0:
            return SuiteResult(tests=1, failures=0, errors=0)
        # One suite as a single pass/fail unit — per-test counts aren't available without a report.
        # Keep the command's own output so a *baseline* failure (a first-run misconfiguration, the
        # common case) can be diagnosed instead of vanishing; tail it to stay bounded.
        detail = (completed.stderr or completed.stdout or "").strip()
        return SuiteResult(tests=1, failures=1, errors=0, detail=detail[-2000:])


def parse_junit_xml(xml: str) -> SuiteResult:
    """Parse JUnit XML (as GdUnit4 emits) into a `SuiteResult`, summing every ``<testsuite>``.

    The XML is the test runner's own output with a fixed structure (no DTD/entities), so stdlib
    ElementTree is used. Raises ``xml.etree.ElementTree.ParseError`` on malformed XML and
    ``ValueError`` if it contains no ``<testsuite>``.
    """
    root = ElementTree.fromstring(xml)
    # Sum direct <testsuite> elements only: a root <testsuite> itself, or the children of a
    # <testsuites>. A <testsuite> may nest child <testsuite>s whose totals already roll up into the
    # parent's attributes, so descending (`.iter`) would double-count `tests`/`skipped`.
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError("no <testsuite> element in JUnit XML")
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.get("tests", "0"))
        failures += int(suite.get("failures", "0"))
        errors += int(suite.get("errors", "0"))
        skipped += int(suite.get("skipped", "0"))
    return SuiteResult(tests=tests, failures=failures, errors=errors, skipped=skipped)
