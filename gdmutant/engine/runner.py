"""The test-runner interface + JUnit-XML result parsing (language-neutral).

The engine drives a `Runner` (Slice 5): apply a mutant, run the target's suite, read a
`SuiteResult`. A mutant is **killed** when the suite fails and **survives** when it passes. JUnit
XML is a cross-language standard, so its parser lives here; the concrete Godot/GdUnit4 runner is a
GDScript-adapter concern that lands with the end-to-end slice (it needs real Godot to validate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from xml.etree import ElementTree


@dataclass(frozen=True)
class SuiteResult:
    """The aggregate outcome of running a test suite once."""

    tests: int
    failures: int
    errors: int
    skipped: int = 0

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
