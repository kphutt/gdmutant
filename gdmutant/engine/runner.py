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
    """Runs a target project's test suite once and reports the aggregate result."""

    def run(self, project_dir: str) -> SuiteResult: ...


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

    def run(self, project_dir: str) -> SuiteResult:
        try:
            completed = subprocess.run(
                list(self.command),
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as timeout:
            # A mutation-induced hang is a detection, not a crash — surface it distinctly so the
            # engine tallies Timeout (killed), not error. subprocess.run has already killed the
            # child process by the time it raises.
            raise SuiteTimeout(f"test command exceeded {self.timeout:g}s") from timeout
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
